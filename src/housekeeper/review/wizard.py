"""Bulk duplicate review: server-side rule proposals, always previewable.

Rules pick one keeper per group and write ``review_decisions`` only (``MARK_KEEP`` /
``APPROVE_FOR_REVIEW``). Nothing here moves or deletes files.

* Apply re-derives keepers from the database — the client never supplies entry lists.
* Groups are all-or-nothing: if any member fails approval preconditions, the group is skipped.
"""

from __future__ import annotations

import hashlib
import json

from ..database import Database
from .decisions import record_decision

#: Applying more than this in one request is refused; continue with ``next_after_id``.
MAX_GROUPS_PER_REQUEST = 500

RULES = ("keep-canonical", "keep-newest", "keep-under")


def _members(database: Database, group_ids: list[int]) -> dict[int, list[dict]]:
    if not group_ids:
        return {}
    marks = ",".join("?" for _ in group_ids)
    rows = database.fetch_all(
        f"""SELECT m.group_id,e.id,e.relative_path,e.absolute_path,e.size_bytes,e.modified_at
            FROM current_exact_duplicate_members m
            JOIN current_entries e ON e.id=m.entry_id
            WHERE m.group_id IN ({marks}) ORDER BY m.group_id,e.id""",
        tuple(group_ids),
    )
    out: dict[int, list[dict]] = {}
    for row in rows:
        out.setdefault(int(row["group_id"]), []).append(
            {
                "entry_id": int(row["id"]),
                "relative_path": str(row["relative_path"]),
                "absolute_path": str(row["absolute_path"]),
                "size_bytes": int(row["size_bytes"] or 0),
                "modified_at": row["modified_at"],
            }
        )
    return out


def _pick(members: list[dict], canonical_id: int | None, rule: str, path_prefix: str) -> dict | None:
    """The keeper a rule chooses, or ``None`` when the rule does not apply to this group.

    Ties are broken by the canonical choice first and the lowest entry id second, so the same group
    yields the same keeper on every run — a preview a user reads must be the thing that gets applied.
    """
    candidates = members
    if rule == "keep-canonical":
        candidates = [m for m in members if m["entry_id"] == canonical_id]
    elif rule == "keep-under":
        prefix = path_prefix.strip("/")
        if not prefix:
            raise ValueError("keep-under needs a path prefix")
        candidates = [
            m
            for m in members
            if m["relative_path"] == prefix or m["relative_path"].startswith(prefix + "/")
        ]
    elif rule == "keep-newest":
        newest = max((m["modified_at"] or 0) for m in members)
        candidates = [m for m in members if (m["modified_at"] or 0) == newest]
    else:
        raise ValueError(f"unknown rule: {rule}")
    if not candidates:
        return None
    return min(candidates, key=lambda m: (m["entry_id"] != canonical_id, m["entry_id"]))


def _approvable(database: Database, entry_ids: list[int]) -> dict[int, str]:
    """Why each entry cannot be approved, for the ones that cannot. Same rules as record_decision.

    Checked up front for the whole page so a preview shows the same verdict the apply will reach,
    instead of discovering it one exception at a time.
    """
    if not entry_ids:
        return {}
    marks = ",".join("?" for _ in entry_ids)
    rows = database.fetch_all(
        f"""SELECT e.id,e.entry_type,e.scan_status,s.full_hash,s.hash_status,c.classification
            FROM filesystem_entries e
            LEFT JOIN file_signatures s ON s.entry_id=e.id
            LEFT JOIN classifications c ON c.entry_id=e.id
            WHERE e.id IN ({marks})""",
        tuple(entry_ids),
    )
    problems: dict[int, str] = {}
    for row in rows:
        if row["entry_type"] != "file" or row["scan_status"] == "ERROR":
            problems[int(row["id"])] = "not a readable file"
        elif not row["full_hash"] or row["hash_status"] not in {"OK", "VERIFIED"}:
            problems[int(row["id"])] = "no verified full hash"
        elif row["classification"] in {"PROTECTED", "ERROR", "UNKNOWN"}:
            problems[int(row["id"])] = f"classification is {row['classification'] or 'missing'}"
    missing = set(entry_ids) - {int(row["id"]) for row in rows}
    problems.update({entry_id: "entry not found" for entry_id in missing})
    return problems


def _stale_paths(database: Database, session_id: int, paths: list[str]) -> set[str]:
    """Paths this session has a stale decision on, matched by path rather than by entry id.

    A rescan is what makes a decision stale, and a rescan also writes a *new* entry row for the same
    file — so the decision points at the previous snapshot's id. Matching on the path is what lets the
    preview say "you already decided this, and that decision needs re-checking" instead of showing a
    clean slate.
    """
    if not paths:
        return set()
    marks = ",".join("?" for _ in paths)
    rows = database.fetch_all(
        f"""SELECT DISTINCT e.relative_path path FROM review_decisions d
            JOIN filesystem_entries e ON e.id=d.target_id
            WHERE d.review_session_id=? AND d.target_type='ENTRY' AND d.current=1 AND d.stale=1
              AND e.relative_path IN ({marks})""",
        (session_id, *paths),
    )
    return {str(row["path"]) for row in rows}


def plan_fingerprint(groups: list[dict]) -> str:
    """Digest of exactly what a preview showed: per group, the keeper and the proposed approvals.

    This is what binds an apply to the preview a user read. Entry ids are part of it, and a rescan
    writes new entry rows for every file, so any change to the snapshot — or to a group's membership,
    or to which copy the rule picks — changes the digest and the apply is refused.
    """
    material = [
        (
            group["group_id"],
            group["keeper"]["entry_id"] if group["keeper"] else None,
            sorted(member["entry_id"] for member in group["approve"]),
            group["skipped"],
        )
        for group in groups
    ]
    return hashlib.sha256(json.dumps(material, sort_keys=True).encode()).hexdigest()


def preview(
    database: Database,
    rule: str,
    path_prefix: str = "",
    after_id: int = 0,
    limit: int = 100,
    session_id: int | None = None,
) -> dict:
    """What the rule would record, group by group. Reads only — no decision is written."""
    if rule not in RULES:
        raise ValueError(f"unknown rule: {rule}")
    limit = max(1, min(int(limit), MAX_GROUPS_PER_REQUEST))
    groups = database.fetch_all(
        "SELECT id,size_bytes,member_count,canonical_entry_id FROM current_exact_duplicate_groups "
        "WHERE id>? ORDER BY id LIMIT ?",
        (int(after_id), limit),
    )
    members = _members(database, [int(row["id"]) for row in groups])
    every_member = [m["entry_id"] for group in members.values() for m in group]
    problems = _approvable(database, every_member)
    stale = (
        _stale_paths(database, session_id, [m["relative_path"] for g in members.values() for m in g])
        if session_id
        else set()
    )
    rows: list[dict] = []
    for group in groups:
        group_id = int(group["id"])
        canonical = group["canonical_entry_id"] and int(group["canonical_entry_id"])
        group_members = members.get(group_id, [])
        keeper = _pick(group_members, canonical, rule, path_prefix) if group_members else None
        approve = [m for m in group_members if keeper and m["entry_id"] != keeper["entry_id"]]
        blocked = sorted(
            {problems[m["entry_id"]] for m in approve if m["entry_id"] in problems}
        )
        rows.append(
            {
                "group_id": group_id,
                "size_bytes": int(group["size_bytes"] or 0),
                "member_count": int(group["member_count"] or len(group_members)),
                "keeper": keeper,
                "approve": approve,
                # A rule that disagrees with the deterministic canonical is not wrong, but it is
                # worth seeing before it is applied to a thousand groups.
                "conflict": bool(keeper and canonical and keeper["entry_id"] != canonical),
                "stale": sorted(
                    m["entry_id"] for m in group_members if m["relative_path"] in stale
                ),
                "skipped": (
                    "no member matches the rule"
                    if not keeper
                    else "; ".join(blocked)
                    if blocked
                    else None
                ),
            }
        )
    actionable = [row for row in rows if not row["skipped"]]
    return {
        "rule": rule,
        "path_prefix": path_prefix,
        "session_id": session_id,
        "groups": rows,
        "fingerprint": plan_fingerprint(rows),
        "after_id": int(after_id),
        "next_after_id": int(groups[-1]["id"]) if len(groups) == limit else None,
        "limit": limit,
        "counts": {
            "groups": len(rows),
            "actionable_groups": len(actionable),
            "skipped_groups": len(rows) - len(actionable),
            "conflicts": sum(1 for row in rows if row["conflict"]),
            "stale": sum(len(row["stale"]) for row in rows),
            "would_approve": sum(len(row["approve"]) for row in actionable),
            "would_approve_bytes": sum(
                sum(m["size_bytes"] for m in row["approve"]) for row in actionable
            ),
        },
    }


def apply_rule(
    database: Database,
    session_id: int,
    rule: str,
    path_prefix: str = "",
    after_id: int = 0,
    limit: int = MAX_GROUPS_PER_REQUEST,
    source: str = "dashboard-wizard",
    expected_fingerprint: str | None = None,
) -> dict:
    """Record the rule's decisions for one page of groups, re-deriving them from the database.

    Idempotent by construction: ``record_decision`` supersedes a previous current decision for the
    same target rather than adding a second one, and the rule is deterministic, so applying twice
    leaves the same set of current decisions.

    ``expected_fingerprint`` is the ``fingerprint`` of the preview being confirmed. When it is given
    and no longer matches, nothing is written: between preview and confirmation the groups changed
    (typically a rescan), and the approvals would land on entries nobody looked at. Both HTTP
    endpoints require it; ``None`` means "apply whatever the rule says right now" and exists for a
    caller that has just computed the plan itself.
    """
    if int(limit) > MAX_GROUPS_PER_REQUEST:
        raise ValueError(f"at most {MAX_GROUPS_PER_REQUEST} groups per request")
    plan = preview(database, rule, path_prefix, after_id, limit, session_id)
    if expected_fingerprint and expected_fingerprint != plan["fingerprint"]:
        raise ValueError(
            "the preview is out of date: these duplicate groups changed since it was shown "
            "(a rescan, or another decision) — preview again and re-read it before applying"
        )
    approved = kept = 0
    for group in plan["groups"]:
        if group["skipped"]:
            continue
        reason = f"wizard:{rule}"
        record_decision(
            database,
            session_id,
            "ENTRY",
            group["keeper"]["entry_id"],
            "MARK_KEEP",
            reason=reason,
            source=source,
        )
        kept += 1
        for member in group["approve"]:
            record_decision(
                database,
                session_id,
                "ENTRY",
                member["entry_id"],
                "APPROVE_FOR_REVIEW",
                reason=reason,
                source=source,
            )
            approved += 1
    return {
        **plan["counts"],
        "rule": rule,
        "session_id": session_id,
        "kept": kept,
        "approved": approved,
        "after_id": int(after_id),
        "next_after_id": plan["next_after_id"],
        "fingerprint": plan["fingerprint"],
        "skipped": [
            {"group_id": group["group_id"], "reason": group["skipped"]}
            for group in plan["groups"]
            if group["skipped"]
        ],
    }
