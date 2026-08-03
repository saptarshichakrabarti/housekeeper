"""Preservation-risk analysis: a workflow separate from clutter review.

Flags obsolescence, parser failure, encryption, unknown containers, missing context. Never
lowers retention or becomes a deletion candidate — recommended actions are migration/docs.
"""

from __future__ import annotations

import json

_LEGACY_OFFICE = {".doc", ".xls", ".ppt", ".wpd", ".wks"}
_ENCRYPTED = {".gpg", ".kdbx", ".axx", ".enc"}
_DISK_IMAGE = {".dmg", ".iso", ".vmdk", ".vdi", ".img", ".qcow2"}
_DATABASE = {".sqlite", ".db", ".mdb", ".accdb"}
_EMAIL = {".pst", ".ost", ".mbox", ".eml"}


def assess_entry(row) -> dict[str, str] | None:
    suffix = (row["suffix"] or "").lower()
    risks = {
        "format_risk": "none",
        "integrity_risk": "none",
        "context_loss_risk": "none",
        "accessibility_risk": "none",
        "encryption_risk": "none",
        "application_dependency_risk": "none",
    }
    action = "KEEP_WITH_CHECKSUM"
    if row["scan_status"] == "ERROR" or row["analysis_failed"]:
        risks["integrity_risk"] = "high"
        action = "NEEDS_INTEGRITY_REVIEW"
    if suffix in _LEGACY_OFFICE:
        risks["format_risk"] = "medium"
        risks["application_dependency_risk"] = "medium"
        action = "KEEP_AND_MIGRATE"
    if suffix in _ENCRYPTED:
        risks["encryption_risk"] = "high"
        action = "NEEDS_KEY_DOCUMENTATION"
    if suffix in _DISK_IMAGE:
        risks["accessibility_risk"] = "medium"
        risks["context_loss_risk"] = "medium"
        action = "NEEDS_FORMAT_IDENTIFICATION"
    if suffix in _DATABASE:
        risks["application_dependency_risk"] = "medium"
        action = "KEEP_WITH_APPLICATION_CONTEXT"
    if suffix in _EMAIL:
        risks["application_dependency_risk"] = "high"
        action = "KEEP_WITH_APPLICATION_CONTEXT"
    if all(value == "none" for value in risks.values()):
        return None  # nothing noteworthy; keep the preservation queue meaningful
    return {**risks, "recommended_action": action}


def run_preservation_risk_analysis(database, config, scope=None, job_id=None) -> dict[str, int]:
    from ..jobs import checkpoint

    if not config.section("preservation").get("enabled", True):
        return {"assessed": 0}
    from .scope import resolve_scope

    entry_sql, params = resolve_scope(database, scope).entry_id_sql()
    conn = database.connect()
    conn.execute(
        f"DELETE FROM preservation_assessments WHERE target_type='ENTRY' AND target_id IN ({entry_sql})",
        params,
    )
    assessed = 0
    scanned = 0
    for row in database.iter_rows(
        f"""SELECT e.id,e.suffix,e.scan_status,
                  EXISTS(SELECT 1 FROM entry_content_links l JOIN analysis_artifacts a ON a.content_object_id=l.content_object_id
                         WHERE l.entry_id=e.id AND a.status IN ('ERROR','UNSUPPORTED')) AS analysis_failed
           FROM filesystem_entries e WHERE e.entry_type='file' AND e.id IN ({entry_sql})""",
        params,
    ):
        scanned += 1
        if scanned % 256 == 0:
            checkpoint(database, job_id, processed_count=scanned, state={"assessed": assessed})
        assessment = assess_entry(row)
        if assessment is None:
            continue
        conn.execute(
            """INSERT INTO preservation_assessments(target_type,target_id,format_risk,integrity_risk,context_loss_risk,accessibility_risk,encryption_risk,application_dependency_risk,recommended_action,evidence_json)
               VALUES('ENTRY',?,?,?,?,?,?,?,?,?)
               ON CONFLICT(target_type,target_id) DO UPDATE SET format_risk=excluded.format_risk,integrity_risk=excluded.integrity_risk,
               context_loss_risk=excluded.context_loss_risk,accessibility_risk=excluded.accessibility_risk,encryption_risk=excluded.encryption_risk,
               application_dependency_risk=excluded.application_dependency_risk,recommended_action=excluded.recommended_action""",
            (
                int(row["id"]),
                assessment["format_risk"],
                assessment["integrity_risk"],
                assessment["context_loss_risk"],
                assessment["accessibility_risk"],
                assessment["encryption_risk"],
                assessment["application_dependency_risk"],
                assessment["recommended_action"],
                json.dumps({"suffix": row["suffix"]}),
            ),
        )
        assessed += 1
    conn.commit()
    return {"assessed": assessed}
