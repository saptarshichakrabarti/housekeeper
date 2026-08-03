"""Advisory lifecycle-state assignment (non-binary clutter decisions).

States beyond keep/delete (ACTIVE, ARCHIVE, COLD_ARCHIVE, …). Advisory only — never moves
or deletes files.
"""

from __future__ import annotations

import json
import time

from ..constants import LifecycleState

_TWO_YEARS = 2 * 365 * 24 * 3600


def assign_state(classification: str, modified_at, now: float) -> tuple[str, str]:
    if classification in {"PROTECTED"}:
        return LifecycleState.PROTECTED, "PROTECT"
    if classification in {"ERROR", "UNKNOWN"}:
        return LifecycleState.DEFERRED, "DEFER"
    if classification.startswith("REVIEW_"):
        return LifecycleState.MANUAL_REVIEW, "MOVE_TO_MANUAL_REVIEW"
    age = (now - modified_at) if modified_at else 0
    if age > _TWO_YEARS:
        return LifecycleState.COLD_ARCHIVE, "DEMOTE_TO_COLD_ARCHIVE"
    return LifecycleState.ARCHIVE, "CONSOLIDATE_IN_ARCHIVE"


def run_lifecycle_analysis(database, config, scope=None, job_id=None) -> dict[str, int]:
    from ..jobs import checkpoint
    from .scope import resolve_scope

    now = time.time()
    entry_sql, params = resolve_scope(database, scope).entry_id_sql()
    conn = database.connect()
    # Scoped: `DELETE FROM entry_lifecycle` discarded every other source root's states as well.
    conn.execute(f"DELETE FROM entry_lifecycle WHERE entry_id IN ({entry_sql})", params)
    counts: dict[str, int] = {}
    rows = database.iter_rows(
        f"""SELECT e.id,e.modified_at,COALESCE(c.classification,'KEEP') AS classification
           FROM filesystem_entries e LEFT JOIN classifications c ON c.entry_id=e.id
           WHERE e.entry_type='file' AND e.id IN ({entry_sql})""",
        params,
    )
    for scanned, row in enumerate(rows, start=1):
        if scanned % 256 == 0:
            checkpoint(database, job_id, processed_count=scanned)
        state, recommendation = assign_state(row["classification"], row["modified_at"], now)
        counts[str(state)] = counts.get(str(state), 0) + 1
        conn.execute(
            "INSERT OR REPLACE INTO entry_lifecycle(entry_id,state,recommendation,evidence_json) VALUES(?,?,?,?)",
            (int(row["id"]), state, recommendation, json.dumps({"classification": row["classification"]})),
        )
    conn.commit()
    return counts
