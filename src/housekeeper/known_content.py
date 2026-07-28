"""Local, auditable known-content assertion registry.

Assertions record what the user (or a local rule) knows about content — regenerable, an
installer, an OS cache, a preservation master, a test fixture, etc. They are surfaced during
review as additional signals but are strictly advisory: a global public hash list must never
automatically authorize review movement.
"""

from __future__ import annotations

import json

ASSERTIONS = {
    "KNOWN_REGENERABLE",
    "KNOWN_INSTALLER",
    "KNOWN_OS_CACHE",
    "KNOWN_PRESERVATION_MASTER",
    "KNOWN_CANONICAL_ARCHIVE",
    "KNOWN_LEGACY_REQUIREMENT",
    "KNOWN_TEST_FIXTURE",
}
SCOPE_TYPES = {"CONTENT_OBJECT", "PATH_PATTERN", "PROJECT", "SOURCE_ROOT", "RECORD_SERIES"}


def add_assertion(
    database,
    assertion: str,
    scope_type: str,
    scope_value: str,
    evidence: dict | None = None,
    source: str = "user",
) -> int:
    if assertion not in ASSERTIONS:
        raise ValueError(f"unknown assertion: {assertion}")
    if scope_type not in SCOPE_TYPES:
        raise ValueError(f"unknown scope type: {scope_type}")
    database.connect().execute(
        """INSERT OR IGNORE INTO known_content_assertions(assertion,scope_type,scope_value,evidence_json,source)
           VALUES(?,?,?,?,?)""",
        (assertion, scope_type, scope_value, json.dumps(evidence or {}, sort_keys=True), source),
    )
    database.connect().commit()
    row = database.fetch_one(
        "SELECT id FROM known_content_assertions WHERE assertion=? AND scope_type=? AND scope_value=?",
        (assertion, scope_type, scope_value),
    )
    assert row is not None
    return int(row["id"])


def list_assertions(database):
    return database.fetch_all(
        "SELECT id,assertion,scope_type,scope_value,source,created_at FROM known_content_assertions ORDER BY id DESC"
    )


def assertions_for_entry(database, entry_id: int) -> list[str]:
    """Advisory: which assertions apply to an entry, via path pattern or content object."""
    entry = database.fetch_one(
        """SELECT e.relative_path, l.content_object_id FROM filesystem_entries e
           LEFT JOIN entry_content_links l ON l.entry_id=e.id WHERE e.id=?""",
        (entry_id,),
    )
    if not entry:
        return []
    result: list[str] = []
    for row in database.iter_rows("SELECT assertion,scope_type,scope_value FROM known_content_assertions"):
        if row["scope_type"] == "PATH_PATTERN" and row["scope_value"] in (entry["relative_path"] or "") or (
            row["scope_type"] == "CONTENT_OBJECT"
            and entry["content_object_id"] is not None
            and str(entry["content_object_id"]) == row["scope_value"]
        ):
            result.append(row["assertion"])
    return result
