"""Assign and query role-based canonical preservation copies."""

from __future__ import annotations

import json

from ..constants import CanonicalRole


def _representative_entry(database, content_object_id: int) -> int | None:
    row = database.fetch_one(
        """SELECT e.id FROM entry_content_links l JOIN filesystem_entries e ON e.id=l.entry_id
           WHERE l.content_object_id=? AND e.entry_type='file' ORDER BY e.id LIMIT 1""",
        (content_object_id,),
    )
    return int(row["id"]) if row else None


def _assign(database, group_type, group_id, role, entry_id, content_object_id, score, components):
    database.connect().execute(
        """INSERT INTO canonical_assignments(target_group_type,target_group_id,canonical_role,entry_id,content_object_id,score,score_components_json,source)
           VALUES(?,?,?,?,?,?,?, 'analyser')
           ON CONFLICT(target_group_type,target_group_id,canonical_role,entry_id)
           DO UPDATE SET score=excluded.score,score_components_json=excluded.score_components_json,superseded_at=NULL""",
        (
            group_type,
            group_id,
            role,
            entry_id,
            content_object_id,
            score,
            json.dumps(components, sort_keys=True),
        ),
    )
    database.connect().commit()


def assign_location_roles(database) -> int:
    """Give every exact-duplicate group's canonical entry the CANONICAL_LOCATION role."""
    assigned = 0
    for row in database.iter_rows(
        "SELECT id,canonical_entry_id FROM exact_duplicate_groups WHERE canonical_entry_id IS NOT NULL"
    ):
        content = database.fetch_one(
            "SELECT content_object_id FROM entry_content_links WHERE entry_id=?",
            (row["canonical_entry_id"],),
        )
        _assign(
            database,
            "EXACT_DUPLICATE_GROUP",
            int(row["id"]),
            CanonicalRole.CANONICAL_LOCATION,
            int(row["canonical_entry_id"]),
            content["content_object_id"] if content else None,
            1.0,
            {"reason": "exact_duplicate_canonical"},
        )
        assigned += 1
    return assigned


def _pixel_identical_components(database) -> list[list[int]]:
    """Connected components of PIXEL_IDENTICAL content-object relationships (union-find)."""
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: int, b: int) -> None:
        parent[find(a)] = find(b)

    for row in database.iter_rows(
        "SELECT source_id,target_id FROM content_relationships WHERE relationship_type='PIXEL_IDENTICAL' AND status='ACTIVE'"
    ):
        union(int(row["source_id"]), int(row["target_id"]))
    groups: dict[int, list[int]] = {}
    for node in list(parent):
        groups.setdefault(find(node), []).append(node)
    return [sorted(members) for members in groups.values() if len(members) >= 2]


def assign_image_metadata_roles(database) -> int:
    """For each pixel-identical image group, protect the richest-metadata / highest-fidelity copy."""
    profile = database.fetch_one(
        "SELECT id FROM normalization_profiles WHERE name='IMAGE_PIXEL_EQUIVALENCE' ORDER BY id DESC LIMIT 1"
    )
    if not profile:
        return 0
    assigned = 0
    for component in _pixel_identical_components(database):
        best_fidelity = None
        best_metadata = None
        for cid in component:
            row = database.fetch_one(
                "SELECT artifact_json FROM normalized_content_artifacts WHERE content_object_id=? AND normalization_profile_id=? AND status='OK'",
                (cid, profile["id"]),
            )
            if not row:
                continue
            info = json.loads(row["artifact_json"] or "{}")
            pixels = int(info.get("width", 0)) * int(info.get("height", 0))
            exif = bool(info.get("exif_present"))
            if best_fidelity is None or pixels > best_fidelity[1]:
                best_fidelity = (cid, pixels)
            if exif and (best_metadata is None or pixels > best_metadata[1]):
                best_metadata = (cid, pixels)
        group_id = component[0]
        if best_fidelity:
            entry = _representative_entry(database, best_fidelity[0])
            _assign(database, "PIXEL_IDENTICAL_GROUP", group_id, CanonicalRole.HIGHEST_FIDELITY_COPY,
                    entry, best_fidelity[0], 1.0, {"pixels": best_fidelity[1]})
            assigned += 1
        metadata_choice = best_metadata or best_fidelity
        if metadata_choice:
            entry = _representative_entry(database, metadata_choice[0])
            _assign(database, "PIXEL_IDENTICAL_GROUP", group_id, CanonicalRole.BEST_METADATA_COPY,
                    entry, metadata_choice[0], 1.0, {"exif_present": bool(best_metadata)})
            assigned += 1
    return assigned


def assign_canonical_roles(database) -> dict[str, int]:
    return {
        "location": assign_location_roles(database),
        "image_metadata": assign_image_metadata_roles(database),
    }


def roles_for_group(database, group_type: str, group_id: int):
    return database.fetch_all(
        "SELECT canonical_role,entry_id,content_object_id,score,score_components_json FROM canonical_assignments WHERE target_group_type=? AND target_group_id=? AND superseded_at IS NULL ORDER BY canonical_role",
        (group_type, group_id),
    )


def roles_lost_if_moved(database, approved_entry_ids: set[int]) -> list[dict]:
    """Roles that would lose *all* their assigned copies if the approved entries were moved.

    A survival-constraint helper for review validation: an approved movement that would remove
    every copy fulfilling a required canonical role must be flagged for explicit acknowledgement.
    """
    lost = []
    rows = database.fetch_all(
        "SELECT target_group_type,target_group_id,canonical_role,entry_id FROM canonical_assignments WHERE superseded_at IS NULL AND entry_id IS NOT NULL"
    )
    by_role: dict[tuple[str, int, str], list[int]] = {}
    for row in rows:
        key = (row["target_group_type"], int(row["target_group_id"]), row["canonical_role"])
        by_role.setdefault(key, []).append(int(row["entry_id"]))
    for (group_type, group_id, role), entries in by_role.items():
        if entries and all(entry in approved_entry_ids for entry in entries):
            lost.append({"group_type": group_type, "group_id": group_id, "role": role, "entries": entries})
    return lost
