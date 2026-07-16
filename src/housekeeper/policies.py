import json

from .config import AppConfig
from .constants import PROTECTED_SUFFIXES, Classification
from .database import Database


def classify_all_entries(database: Database, config: AppConfig) -> None:
    database.connect().execute("DELETE FROM classifications")
    rows = database.fetch_all(
        "SELECT e.id,e.absolute_path,e.suffix,e.scan_status,s.full_hash,g.canonical_entry_id FROM filesystem_entries e LEFT JOIN file_signatures s ON s.entry_id=e.id LEFT JOIN exact_duplicate_members m ON m.entry_id=e.id LEFT JOIN exact_duplicate_groups g ON g.id=m.group_id WHERE e.entry_type='file'"
    )
    for r in rows:
        cls = Classification.KEEP
        conf = 0.8
        reasons = []
        explanation = "No safe removal recommendation."
        if r["scan_status"] == "ERROR":
            cls = Classification.ERROR
            conf = 1.0
            reasons = ["PARSER_OR_FILESYSTEM_ERROR"]
            explanation = "Inspection failed; manual review is required."
        elif r["suffix"] in PROTECTED_SUFFIXES:
            cls = Classification.PROTECTED
            conf = 0.95
            reasons = ["PROTECTED_EXTENSION"]
            explanation = "Conservative protected-file policy."
        elif r["canonical_entry_id"] and r["canonical_entry_id"] != r["id"]:
            cls = Classification.REVIEW_SAFE
            conf = 1.0
            reasons = ["EXACT_SHA256_DUPLICATE", "VERIFIED_CANONICAL_SURVIVES"]
            explanation = "Verified exact duplicate; canonical copy remains."
        database.connect().execute(
            "INSERT INTO classifications(entry_id,classification,confidence,primary_reason_code,reason_codes_json,rule_ids_json,explanation,canonical_entry_id,requires_manual_approval) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                r["id"],
                cls,
                conf,
                reasons[0] if reasons else "KEEP_BY_DEFAULT",
                json.dumps(reasons),
                json.dumps([]),
                explanation,
                r["canonical_entry_id"],
                int(cls not in {Classification.KEEP, Classification.KEEP_CANONICAL}),
            ),
        )
    database.connect().commit()
