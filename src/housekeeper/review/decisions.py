import hashlib
import json
from typing import Any
from ..database import Database


def create_session(
    database: Database, name: str, description: str = "", base_scan_run_id: int | None = None
) -> int:
    cur = database.connect().execute(
        "INSERT INTO review_sessions(name,description,base_scan_run_id) VALUES(?,?,?)",
        (name, description, base_scan_run_id),
    )
    database.connect().commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def record_decision(
    database: Database,
    session_id: int,
    target_type: str,
    target_id: int,
    decision: str,
    user_note: str = "",
    reason: str = "",
    source: str = "cli",
) -> int:
    conn = database.connect()
    session = conn.execute(
        "SELECT status FROM review_sessions WHERE id=?", (session_id,)
    ).fetchone()
    if not session:
        raise ValueError(f"unknown review session {session_id}")
    if session["status"] in {"LOCKED", "EXPORTED", "ARCHIVED"}:
        raise ValueError("review session is immutable")
    if target_type not in {
        "ENTRY",
        "CONTENT_OBJECT",
        "DUPLICATE_GROUP",
        "DIRECTORY_OVERLAP",
        "DOCUMENT_VERSION_GROUP",
        "IMAGE_GROUP",
        "PROJECT",
    }:
        raise ValueError("unsupported review target type")
    if decision == "APPROVE_FOR_REVIEW" and target_type == "ENTRY":
        evidence = conn.execute(
            """SELECT e.entry_type,e.scan_status,s.full_hash,s.hash_status,c.classification
            FROM filesystem_entries e LEFT JOIN file_signatures s ON s.entry_id=e.id LEFT JOIN classifications c ON c.entry_id=e.id WHERE e.id=?""",
            (target_id,),
        ).fetchone()
        if (
            not evidence
            or evidence["entry_type"] != "file"
            or evidence["scan_status"] == "ERROR"
            or not evidence["full_hash"]
            or evidence["hash_status"] not in {"OK", "VERIFIED"}
            or evidence["classification"] in {"PROTECTED", "ERROR", "UNKNOWN"}
        ):
            raise ValueError(
                "approval requires a readable, verified full hash and non-protected classification"
            )
    old = conn.execute(
        "SELECT * FROM review_decisions WHERE review_session_id=? AND target_type=? AND target_id=? AND current=1",
        (session_id, target_type, target_id),
    ).fetchone()
    if old:
        conn.execute(
            "UPDATE review_decisions SET current=0,updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (old["id"],),
        )
    cur = conn.execute(
        "INSERT INTO review_decisions(review_session_id,target_type,target_id,decision,user_note,reason,source) VALUES(?,?,?,?,?,?,?)",
        (session_id, target_type, target_id, decision, user_note, reason, source),
    )
    assert cur.lastrowid is not None
    decision_id = cur.lastrowid
    conn.execute(
        "INSERT INTO review_decision_history(decision_id,review_session_id,target_type,target_id,previous_decision,new_decision,user_note,source) VALUES(?,?,?,?,?,?,?,?)",
        (
            decision_id,
            session_id,
            target_type,
            target_id,
            old["decision"] if old else None,
            decision,
            user_note,
            source,
        ),
    )
    conn.execute(
        "UPDATE review_sessions SET updated_at=CURRENT_TIMESTAMP WHERE id=?", (session_id,)
    )
    conn.commit()
    return decision_id


def mark_stale_for_entry(database: Database, entry_id: int) -> int:
    cur = database.connect().execute(
        "UPDATE review_decisions SET stale=1,updated_at=CURRENT_TIMESTAMP WHERE target_type='ENTRY' AND target_id=? AND current=1",
        (entry_id,),
    )
    database.connect().commit()
    return cur.rowcount


def mark_stale_for_targets(database: Database, target_type: str, target_ids: list[int] | None = None) -> int:
    """Invalidate decisions when an analyzer, policy, or relationship group is recomputed."""
    conn = database.connect()
    if target_ids:
        marks = ",".join("?" for _ in target_ids)
        cur = conn.execute(f"UPDATE review_decisions SET stale=1,updated_at=CURRENT_TIMESTAMP WHERE target_type=? AND target_id IN ({marks}) AND current=1", (target_type, *target_ids))
    else:
        cur = conn.execute("UPDATE review_decisions SET stale=1,updated_at=CURRENT_TIMESTAMP WHERE target_type=? AND current=1", (target_type,))
    conn.commit()
    return cur.rowcount


def export_snapshot(
    database: Database, session_id: int, extra: dict[str, Any] | None = None
) -> int:
    rows = database.fetch_all(
        "SELECT * FROM review_decisions WHERE review_session_id=? AND current=1 ORDER BY id",
        (session_id,),
    )
    run = database.fetch_one("SELECT id,config_hash,completed_at FROM scan_runs WHERE status='COMPLETE' ORDER BY id DESC LIMIT 1")
    artifacts = database.fetch_all("SELECT analyzer_name,analyzer_version,configuration_fingerprint,COUNT(*) AS count FROM analysis_artifacts WHERE status='COMPLETED' GROUP BY analyzer_name,analyzer_version,configuration_fingerprint")
    payload = {
        "session_id": session_id,
        "schema_version": database.database_stats()["schema_version"],
        "decisions": [dict(r) for r in rows],
        "scan": dict(run) if run else None,
        "artifact_versions": [dict(row) for row in artifacts],
        **(extra or {}),
    }
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()
    cur = database.connect().execute(
        "INSERT INTO review_snapshots(review_session_id,snapshot_json,manifest_hash) VALUES(?,?,?)",
        (session_id, json.dumps(payload, sort_keys=True), digest),
    )
    database.connect().commit()
    database.connect().execute(
        "UPDATE review_sessions SET analysis_snapshot_id=?,updated_at=CURRENT_TIMESTAMP WHERE id=?",
        (str(cur.lastrowid), session_id),
    )
    database.connect().commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def session_payload(database: Database, session_id: int) -> dict[str, Any]:
    rows = database.fetch_all(
        "SELECT * FROM review_decisions WHERE review_session_id=? AND current=1 ORDER BY id",
        (session_id,),
    )
    return {
        "session_id": session_id,
        "schema_version": database.database_stats()["schema_version"],
        "decisions": [dict(r) for r in rows],
    }


def validate_session(database: Database, session_id: int) -> list[str]:
    errors: list[str] = []
    session = database.fetch_one("SELECT status FROM review_sessions WHERE id=?", (session_id,))
    if not session:
        return [f"missing review session {session_id}"]
    stale = database.fetch_one(
        "SELECT COUNT(*) AS n FROM review_decisions WHERE review_session_id=? AND current=1 AND stale=1",
        (session_id,),
    )
    if stale and stale["n"]:
        errors.append(f"{stale['n']} stale decisions require revalidation")
    if session["status"] in {"ARCHIVED"}:
        errors.append("review session is archived")
    return errors
