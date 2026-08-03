"""Advisory personal retention policies applied to record series.

Personal guidelines, not legal determinations. Summarizes preserve vs review; never moves
files or weakens protection.
"""

from __future__ import annotations

import json

DEFAULT_POLICIES = {
    "research-retention": {
        "preserve_roles": ["EDITABLE_SOURCE", "FINAL_EXPORT", "PRESERVATION_MASTER"],
        "preserve_kinds": ["SOURCE_CODE", "RAW_DATA", "DOCUMENTATION"],
        "review_kinds": ["CACHE", "REGENERABLE_ENVIRONMENT", "GENERATED_INTERMEDIATE"],
    },
    "default-retention": {
        "preserve_roles": ["PRESERVATION_MASTER"],
        "preserve_kinds": [],
        "review_kinds": ["GENERATED_BUILD_ARTIFACTS", "TEMPORARY_EXPORTS"],
    },
}

# Which default policy applies to which record series.
_SERIES_POLICY = {
    "RESEARCH_PROJECTS": "research-retention",
    "SOURCE_CODE": "research-retention",
    "GENERATED_BUILD_ARTIFACTS": "default-retention",
    "TEMPORARY_EXPORTS": "default-retention",
}


def seed_retention_policies(database) -> None:
    for name, rules in DEFAULT_POLICIES.items():
        database.connect().execute(
            "INSERT OR IGNORE INTO retention_policies(name,version,description,rules_json) VALUES(?,?,?,?)",
            (name, "1", f"Default advisory policy: {name}", json.dumps(rules, sort_keys=True)),
        )
    database.connect().commit()
    for series_name, policy_name in _SERIES_POLICY.items():
        database.connect().execute(
            """UPDATE record_series SET retention_policy_id=(SELECT id FROM retention_policies WHERE name=? AND version='1')
               WHERE name=?""",
            (policy_name, series_name),
        )
    database.connect().commit()


def apply_retention_policies(database, config) -> dict[str, dict[str, int]]:
    """Summarize, per record series with a policy, how many entries preserve vs. review.

    Regenerable review-classified entries in a series whose policy lists a review_kind are
    surfaced as review; protected/error entries are always counted as preserved (fail closed).
    """
    seed_retention_policies(database)
    summary: dict[str, dict[str, int]] = {}
    for series in database.iter_rows(
        """SELECT s.name AS series_name, p.rules_json FROM record_series s
           JOIN retention_policies p ON p.id=s.retention_policy_id"""
    ):
        rules = json.loads(series["rules_json"])
        review_kinds = set(rules.get("review_kinds", []))
        preserve = review = 0
        for entry in database.iter_rows(
            """SELECT c.classification FROM record_series_assignments a
               JOIN record_series s2 ON s2.id=a.series_id
               LEFT JOIN classifications c ON c.entry_id=a.target_id
               WHERE a.target_type='ENTRY' AND s2.name=?""",
            (series["series_name"],),
        ):
            classification = entry["classification"] or "KEEP"
            if classification in ("PROTECTED", "ERROR"):
                preserve += 1
            elif classification.startswith("REVIEW_") and (
                series["series_name"] in review_kinds or review_kinds
            ):
                review += 1
            else:
                preserve += 1
        summary[series["series_name"]] = {"preserve": preserve, "review": review}
    return summary
