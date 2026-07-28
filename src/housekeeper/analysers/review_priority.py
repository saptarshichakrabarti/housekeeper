"""Risk-adjusted review prioritization.

Ranks review candidates with explicit, configurable component scores. The score is never
presented as objective truth; component values and an explanation are stored so a user can see
exactly why something ranked where it did.
"""

from __future__ import annotations

import json
import math

from ..constants import ReviewPriorityCategory

_REGENERABLE = {"PYTHON_BYTECODE_CACHE", "VIRTUAL_ENVIRONMENT", "NODE_MODULES"}


def score_entry(row, weights: dict[str, float], has_preservation_risk: bool):
    classification = row["classification"] or "KEEP"
    reason = row["primary_reason_code"] or ""
    size = int(row["size_bytes"] or 0)
    is_exact_dup = classification == "REVIEW_SAFE" and reason.startswith("EXACT")
    regenerable = reason in _REGENERABLE
    low_risk = is_exact_dup or regenerable
    components = {
        "recoverable_bytes": math.log10(size + 1),
        "redundancy_confidence": 1.0 if is_exact_dup else 0.0,
        "regeneration_confidence": 1.0 if regenerable else 0.0,
        "loss_risk": 0.0 if low_risk else 0.6,
        "preservation_risk": 1.0 if has_preservation_risk else 0.0,
        "review_effort": 0.2 if low_risk else 0.7,
    }
    score = sum(weights.get(key, 0.0) * value for key, value in components.items())
    if has_preservation_risk:
        category = ReviewPriorityCategory.PRESERVATION_FIRST
    elif low_risk:
        category = ReviewPriorityCategory.QUICK_SAFE_WIN
    elif classification in {"REVIEW_BACKUP", "REVIEW_VERSION_FAMILY"}:
        category = ReviewPriorityCategory.CONTEXT_REQUIRED
    elif size >= 512 * 1024 * 1024:
        category = ReviewPriorityCategory.HIGH_VALUE_REVIEW
    else:
        category = ReviewPriorityCategory.LOW_PRIORITY
    return score, category, components


def run_review_priority_analysis(database, config, scope=None, job_id=None) -> dict[str, int]:
    from ..jobs import checkpoint

    weights = config.section("review_priority")["weights"]
    preservation = {
        int(r["target_id"])
        for r in database.iter_rows(
            "SELECT target_id FROM preservation_assessments WHERE target_type='ENTRY' AND recommended_action<>'KEEP_WITH_CHECKSUM'"
        )
    }
    from .scope import resolve_scope

    entry_sql, params = resolve_scope(database, scope).entry_id_sql()
    conn = database.connect()
    # Scoped delete: an unscoped rebuild discarded the priorities of every other source root too.
    conn.execute(
        f"DELETE FROM review_priority WHERE target_type='ENTRY' AND target_id IN ({entry_sql})",
        params,
    )
    counts: dict[str, int] = {}
    rows = database.iter_rows(
        f"""SELECT c.entry_id,c.classification,c.primary_reason_code,e.size_bytes
           FROM classifications c JOIN filesystem_entries e ON e.id=c.entry_id
           WHERE c.classification LIKE 'REVIEW_%' AND e.id IN ({entry_sql})""",
        params,
    )
    for scanned, row in enumerate(rows, start=1):
        if scanned % 256 == 0:
            checkpoint(database, job_id, processed_count=scanned)
        score, category, components = score_entry(
            row, weights, int(row["entry_id"]) in preservation
        )
        counts[str(category)] = counts.get(str(category), 0) + 1
        conn.execute(
            """INSERT INTO review_priority(target_type,target_id,category,score,components_json,explanation)
               VALUES('ENTRY',?,?,?,?,?)
               ON CONFLICT(target_type,target_id) DO UPDATE SET category=excluded.category,score=excluded.score,
               components_json=excluded.components_json,explanation=excluded.explanation""",
            (
                int(row["entry_id"]),
                category,
                score,
                json.dumps(components, sort_keys=True),
                f"{category}: "
                + ", ".join(f"{k}={v:.2f}" for k, v in components.items()),
            ),
        )
    conn.commit()
    return counts
