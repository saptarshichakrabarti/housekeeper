import json

from ..database import Database
from .decisions import record_decision


def override_canonical(database: Database, session_id: int, group_id: int, entry_id: int) -> None:
    group = database.fetch_one(
        "SELECT member_count FROM exact_duplicate_groups WHERE id=?", (group_id,)
    )
    member = database.fetch_one(
        "SELECT readable FROM exact_duplicate_members WHERE group_id=? AND entry_id=?",
        (group_id, entry_id),
    )
    entry = database.fetch_one(
        "SELECT scan_status,size_bytes FROM filesystem_entries WHERE id=? AND entry_type='file'",
        (entry_id,),
    )
    if (
        not group
        or not member
        or not entry
        or not member["readable"]
        or entry["scan_status"] == "ERROR"
    ):
        raise ValueError("canonical must be a readable member of the duplicate group")
    if group["member_count"] < 2:
        raise ValueError("canonical override requires another surviving copy")
    database.connect().execute(
        "UPDATE exact_duplicate_groups SET canonical_entry_id=?,canonical_selection_reason='user override' WHERE id=?",
        (entry_id, group_id),
    )
    database.connect().execute(
        "UPDATE exact_duplicate_members SET is_canonical=CASE WHEN entry_id=? THEN 1 ELSE 0 END WHERE group_id=?",
        (entry_id, group_id),
    )
    database.connect().execute(
        "INSERT OR REPLACE INTO canonical_overrides(duplicate_group_id,canonical_entry_id,review_session_id,evidence_json) VALUES(?,?,?,?)",
        (
            group_id,
            entry_id,
            session_id,
            json.dumps(
                {"validated_member": True, "surviving_member_count": group["member_count"] - 1}
            ),
        ),
    )
    database.connect().commit()
    record_decision(
        database,
        session_id,
        "DUPLICATE_GROUP",
        group_id,
        "CHANGE_CANONICAL",
        reason="validated user canonical override",
    )
