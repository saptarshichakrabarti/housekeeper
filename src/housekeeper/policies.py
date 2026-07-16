import json

from .config import AppConfig
from .constants import PROTECTED_SUFFIXES, Classification
from .database import Database
from .core.database_writer import DatabaseWriter


def classify_all_entries(database: Database, config: AppConfig) -> None:
    database.connect().execute("DELETE FROM classifications")
    rows = database.fetch_all(
        """SELECT e.id,e.absolute_path,e.suffix,e.scan_status,s.full_hash,g.canonical_entry_id,
           EXISTS(SELECT 1 FROM entry_content_links l JOIN analysis_artifacts a ON a.content_object_id=l.content_object_id
                  WHERE l.entry_id=e.id AND a.status IN ('ERROR','UNSUPPORTED')) AS analysis_failed
           FROM filesystem_entries e LEFT JOIN file_signatures s ON s.entry_id=e.id
           LEFT JOIN exact_duplicate_members m ON m.entry_id=e.id LEFT JOIN exact_duplicate_groups g ON g.id=m.group_id
           WHERE e.entry_type='file'"""
    )
    sql = "INSERT INTO classifications(entry_id,classification,confidence,primary_reason_code,reason_codes_json,rule_ids_json,explanation,canonical_entry_id,requires_manual_approval) VALUES(?,?,?,?,?,?,?,?,?)"
    performance = config.section("performance")
    with DatabaseWriter(database, int(performance["batch_size"]), int(performance["database_writer_queue_size"])) as writer:
        for r in rows:
            cls = Classification.KEEP
            conf = 0.8
            reasons = []
            explanation = "No safe removal recommendation."
            if r["scan_status"] == "ERROR" or r["analysis_failed"]:
                cls = Classification.ERROR
                conf = 1.0
                reasons = ["PARSER_OR_FILESYSTEM_ERROR"]
                explanation = "Inspection or content analysis failed; manual review is required."
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
            writer.submit(sql, (r["id"], cls, conf, reasons[0] if reasons else "KEEP_BY_DEFAULT", json.dumps(reasons), json.dumps([]), explanation, r["canonical_entry_id"], int(cls not in {Classification.KEEP, Classification.KEEP_CANONICAL})))
