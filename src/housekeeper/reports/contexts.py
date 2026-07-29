"""Per-report context builders. Each returns a plain dict for a Jinja template."""

from __future__ import annotations

import json

from .formatting import display_path, display_source, redacts_paths

_REVIEW_CLASSES = ("REVIEW_SAFE", "REVIEW_PROBABLE", "REVIEW_BACKUP", "REVIEW_LARGE", "REVIEW_VERSION_FAMILY")


def _scan_identity(database, redact: bool = False) -> dict:
    row = database.fetch_one(
        "SELECT id,source_root,source_root_fingerprint,started_at,completed_at,status,config_hash,files_seen,directories_seen,symlinks_seen,errors_seen,bytes_seen FROM scan_runs ORDER BY id DESC LIMIT 1"
    )
    if not row:
        return {}
    identity = dict(row)
    # The fingerprint stays: it identifies the drive without naming where it was mounted, which is
    # exactly what a report needs to prove two runs describe the same source.
    identity["source_root"] = display_source(identity.get("source_root"), redact)
    return identity


def build_summary_context(database, config) -> dict:
    identity = _scan_identity(database, redacts_paths(config))
    files = database.fetch_one(
        "SELECT COUNT(*) n,COALESCE(SUM(size_bytes),0) b FROM current_entries WHERE entry_type='file'"
    )
    classifications = {
        r["classification"]: r["n"]
        for r in database.fetch_all(
            "SELECT classification,COUNT(*) n FROM current_classifications GROUP BY classification"
        )
    }
    reviewable = {
        r["classification"]: {"count": r["n"], "bytes": r["b"]}
        for r in database.fetch_all(
            """SELECT c.classification,COUNT(*) n,COALESCE(SUM(e.size_bytes),0) b
               FROM current_classifications c JOIN current_entries e ON e.id=c.entry_id
               WHERE c.classification LIKE 'REVIEW_%' GROUP BY c.classification"""
        )
    }
    dup = database.fetch_one(
        "SELECT COUNT(*) groups, COALESCE(SUM(member_count-1),0) redundant_members FROM current_exact_duplicate_groups"
    )
    dup_bytes = database.fetch_one(
        """SELECT COALESCE(SUM(g.size_bytes*(g.member_count-1)),0) b FROM current_exact_duplicate_groups g"""
    )
    return {
        "title": "Summary",
        "identity": identity,
        "config_fingerprint": (identity.get("config_hash") or "")[:16],
        "file_count": files["n"],
        "byte_count": files["b"],
        "unreadable": database.fetch_one(
            "SELECT COUNT(*) n FROM current_entries WHERE scan_status='ERROR'"
        )["n"],
        "classifications": classifications,
        "protected": classifications.get("PROTECTED", 0),
        "unknown": classifications.get("UNKNOWN", 0),
        "errors": classifications.get("ERROR", 0),
        "exact_duplicate_groups": dup["groups"],
        "exact_duplicate_bytes": dup_bytes["b"],
        "backup_overlaps": database.fetch_one(
            "SELECT COUNT(*) n FROM current_relationships WHERE relationship_type='MOSTLY_CONTAINED_IN'"
        )["n"],
        "document_version_groups": database.fetch_one(
            "SELECT COUNT(*) n FROM current_relationship_groups WHERE group_type='DOCUMENT_FAMILY'"
        )["n"],
        "image_groups": database.fetch_one(
            "SELECT COUNT(*) n FROM current_relationship_groups WHERE group_type='IMAGE_SIMILARITY'"
        )["n"],
        "projects": database.fetch_one("SELECT COUNT(*) n FROM current_projects")["n"],
        "content_objects": database.fetch_one("SELECT COUNT(*) n FROM current_content_objects")["n"],
        "reviewable_by_class": reviewable,
        "content_relationships": database.fetch_all(
            "SELECT relationship_type,evidence_tier,COUNT(*) n FROM current_content_relationships WHERE status='ACTIVE' GROUP BY relationship_type,evidence_tier ORDER BY evidence_tier"
        ),
    }


def build_inventory_context(database, config) -> dict:
    directories = database.fetch_all(
        """SELECT source_root,
              CASE WHEN instr(relative_path,'/')=0 THEN relative_path ELSE substr(relative_path,1,instr(relative_path,'/')-1) END AS top_level,
              COUNT(*) n, COALESCE(SUM(size_bytes),0) b
           FROM current_entries WHERE entry_type='file'
           GROUP BY source_root,top_level ORDER BY b DESC LIMIT 200"""
    )
    extensions = database.fetch_all(
        "SELECT COALESCE(NULLIF(suffix,''),'(none)') suffix,COUNT(*) n,COALESCE(SUM(size_bytes),0) b FROM current_entries WHERE entry_type='file' GROUP BY suffix ORDER BY b DESC LIMIT 50"
    )
    redact = redacts_paths(config)
    directories = [
        {**dict(row), "source_root": display_source(row["source_root"], redact)}
        for row in directories
    ]
    return {"title": "Inventory", "directories": directories, "extensions": extensions}


def build_duplicates_context(database, config) -> dict:
    redact = redacts_paths(config)
    groups = []
    for group in database.iter_rows(
        "SELECT id,full_hash,size_bytes,member_count,canonical_entry_id FROM current_exact_duplicate_groups ORDER BY size_bytes*(member_count-1) DESC LIMIT 500"
    ):
        members = [
            {
                "relative_path": row["relative_path"],
                "absolute_path": display_path(row["absolute_path"], row["relative_path"], redact),
                "is_canonical": row["is_canonical"],
            }
            for row in database.fetch_all(
                """SELECT e.relative_path,e.absolute_path,
                          CASE WHEN m.entry_id=g.canonical_entry_id THEN 1 ELSE 0 END is_canonical
                   FROM current_exact_duplicate_members m
                   JOIN current_exact_duplicate_groups g ON g.id=m.group_id
                   JOIN current_entries e ON e.id=m.entry_id
                   WHERE m.group_id=? ORDER BY is_canonical DESC,e.relative_path""",
                (group["id"],),
            )
        ]
        groups.append(
            {
                "hash": group["full_hash"][:16],
                "size_bytes": group["size_bytes"],
                "member_count": group["member_count"],
                "redundant_bytes": group["size_bytes"] * (group["member_count"] - 1),
                "members": members,
            }
        )
    return {"title": "Exact duplicates", "groups": groups}


def build_directory_overlap_context(database, config) -> dict:
    rows = database.fetch_all(
        """SELECT a.relative_path a_path, b.relative_path b_path, r.confidence, r.evidence_json
           FROM current_relationships r JOIN current_entries a ON a.id=r.source_id JOIN current_entries b ON b.id=r.target_id
           WHERE r.relationship_type='MOSTLY_CONTAINED_IN' ORDER BY r.confidence DESC LIMIT 300"""
    )
    overlaps = [{**dict(r), "evidence": json.loads(r["evidence_json"] or "{}")} for r in rows]
    return {"title": "Directory overlap", "overlaps": overlaps}


def build_document_versions_context(database, config) -> dict:
    rows = database.fetch_all(
        """SELECT a.name a_name, b.name b_name, r.confidence FROM current_relationships r
           JOIN current_entries a ON a.id=r.source_id JOIN current_entries b ON b.id=r.target_id
           WHERE r.relationship_type='LIKELY_VERSION_OF' ORDER BY r.confidence DESC LIMIT 300"""
    )
    families = database.fetch_all(
        "SELECT group_key,evidence_json FROM current_relationship_groups WHERE group_type='DOCUMENT_FAMILY' LIMIT 200"
    )
    return {"title": "Document versions", "pairs": rows, "families": families}


def build_image_groups_context(database, config) -> dict:
    perceptual = database.fetch_all(
        "SELECT group_key FROM current_relationship_groups WHERE group_type='IMAGE_SIMILARITY' LIMIT 200"
    )
    pixel = database.fetch_all(
        "SELECT COUNT(*) n FROM current_content_relationships WHERE relationship_type='PIXEL_IDENTICAL' AND status='ACTIVE'"
    )
    return {"title": "Image groups", "perceptual_groups": perceptual, "pixel_identical": pixel[0]["n"]}


def build_large_files_context(database, config) -> dict:
    threshold = config.section("reporting")["large_file_threshold_bytes"]
    redact = redacts_paths(config)
    rows = [
        {
            "relative_path": row["relative_path"],
            "absolute_path": display_path(row["absolute_path"], row["relative_path"], redact),
            "size_bytes": row["size_bytes"],
        }
        for row in database.fetch_all(
            "SELECT relative_path,absolute_path,size_bytes FROM current_entries WHERE entry_type='file' AND size_bytes>=? ORDER BY size_bytes DESC LIMIT 500",
            (threshold,),
        )
    ]
    return {"title": "Large files", "threshold": threshold, "files": rows}


def build_projects_context(database, config) -> dict:
    rows = database.fetch_all(
        "SELECT name,kind,markers_json,source_size_bytes,generated_size_bytes,environment_size_bytes,git_status FROM current_projects ORDER BY generated_size_bytes+environment_size_bytes DESC LIMIT 300"
    )
    projects = [{**dict(r), "markers": json.loads(r["markers_json"] or "[]")} for r in rows]
    return {"title": "Projects", "projects": projects}


def build_errors_context(database, config) -> dict:
    scan_errors = database.fetch_all(
        "SELECT relative_path,read_error FROM current_entries WHERE scan_status='ERROR' LIMIT 500"
    )
    parser_errors = database.fetch_all(
        """SELECT a.analyser_name,a.error_code,a.error_message,COUNT(*) n FROM current_analysis_artifacts a
           WHERE a.status IN ('ERROR','UNSUPPORTED') GROUP BY a.analyser_name,a.error_code ORDER BY n DESC LIMIT 200"""
    )
    return {"title": "Errors", "scan_errors": scan_errors, "parser_errors": parser_errors}


#: Buckets of ``scan_entry_changes.change_status``, in the order a reader cares about them.
_CHANGE_BUCKETS = ("NEW", "CONTENT_POSSIBLY_CHANGED", "METADATA_CHANGED", "MISSING", "ERROR")


def _run_totals(database, scan_run_id: int) -> dict:
    """The figures a digest compares between runs, for one snapshot.

    Duplicate groups are counted from that snapshot's own **content links**, not from
    ``exact_duplicate_members``: the analyser replaces a group's membership with the members of the
    snapshot it last ran over, so counting stored members would report an older run as having none and
    invent a change nobody made. Verified links, by contrast, are copied forward per entry and stay
    with the snapshot that recorded them — which makes the two runs genuinely comparable, and uses the
    same definition of "a duplicate group within this scope" as the analyser itself: one content object
    reachable from two or more verified files of the run.

    ``identity_available`` is the one honest escape hatch: a snapshot nobody ever hashed has no groups
    to count, and that is not the same as having none.
    """
    groups = database.fetch_one(
        "SELECT COUNT(*) n FROM (SELECT l.content_object_id FROM entry_content_links l "
        "JOIN filesystem_entries e ON e.id=l.entry_id "
        "WHERE e.scan_run_id=? AND e.entry_type='file' AND l.link_status='VERIFIED' "
        "GROUP BY l.content_object_id HAVING COUNT(*)>1)",
        (scan_run_id,),
    )
    identity = database.fetch_one(
        "SELECT EXISTS(SELECT 1 FROM entry_content_links l "
        "JOIN filesystem_entries e ON e.id=l.entry_id "
        "WHERE e.scan_run_id=? AND l.link_status='VERIFIED') x",
        (scan_run_id,),
    )
    reviewable = database.fetch_one(
        "SELECT COALESCE(SUM(e.size_bytes),0) b FROM classifications c "
        "JOIN filesystem_entries e ON e.id=c.entry_id "
        "WHERE e.scan_run_id=? AND c.classification LIKE 'REVIEW_%'",
        (scan_run_id,),
    )
    return {
        "duplicate_groups": int(groups["n"]) if groups else 0,
        "identity_available": bool(identity and identity["x"]),
        "reviewable_bytes": int(reviewable["b"]) if reviewable else 0,
    }


def changes_digest(database, config=None) -> dict:
    """What changed between the newest scan and the one before it, for the same source.

    Everything here is already recorded — ``scan_entry_changes`` per scan, classifications and
    duplicate groups per snapshot. The digest is the view of it nobody had.

    Honest about having nothing to say: a first scan of a source records no comparison at all, and a
    purge removes the history a comparison would need. Both render as a stated reason rather than a
    diff full of zeroes or an inventory that looks entirely new.
    """
    redact = redacts_paths(config) if config else False
    current = database.fetch_one(
        "SELECT id,source_root,source_root_fingerprint,completed_at FROM scan_runs "
        "WHERE status='COMPLETE' ORDER BY id DESC LIMIT 1"
    )
    if not current:
        return {"title": "Changes", "unavailable": "No completed scan yet."}
    previous = database.fetch_one(
        "SELECT id,completed_at FROM scan_runs WHERE status='COMPLETE' AND id<? "
        "AND source_root_fingerprint=? ORDER BY id DESC LIMIT 1",
        (current["id"], current["source_root_fingerprint"]),
    )
    identity = {
        "scan_run_id": int(current["id"]),
        "source_root": display_source(current["source_root"], redact),
        "completed_at": current["completed_at"],
    }
    if not previous:
        purge = database.fetch_one(
            "SELECT completed_at FROM jobs WHERE job_type='PURGE' AND status='COMPLETED' "
            "ORDER BY id DESC LIMIT 1"
        )
        reason = (
            f"History was purged on {purge['completed_at']}, so there is no earlier scan to "
            "compare against — this is a fresh baseline, not a drive full of new files."
            if purge
            else "This is the first scan of this source, so there is no previous scan to compare."
        )
        return {"title": "Changes", "identity": identity, "unavailable": reason}
    buckets = {
        row["change_status"]: {"count": int(row["n"]), "bytes": int(row["b"])}
        for row in database.fetch_all(
            """SELECT ch.change_status,COUNT(*) n,COALESCE(SUM(e.size_bytes),0) b
               FROM scan_entry_changes ch JOIN filesystem_entries e ON e.id=ch.entry_id
               WHERE ch.scan_run_id=? GROUP BY ch.change_status""",
            (int(current["id"]),),
        )
    }
    largest = {
        status: [
            {
                "relative_path": row["relative_path"],
                "absolute_path": display_path(row["absolute_path"], row["relative_path"], redact),
                "size_bytes": int(row["size_bytes"] or 0),
            }
            for row in database.fetch_all(
                """SELECT ch.relative_path,e.absolute_path,e.size_bytes
                   FROM scan_entry_changes ch JOIN filesystem_entries e ON e.id=ch.entry_id
                   WHERE ch.scan_run_id=? AND ch.change_status=? AND e.entry_type='file'
                   ORDER BY e.size_bytes DESC LIMIT 10""",
                (int(current["id"]), status),
            )
        ]
        for status in _CHANGE_BUCKETS
        if buckets.get(status)
    }
    now, before = _run_totals(database, int(current["id"])), _run_totals(database, int(previous["id"]))
    # A delta only where both sides are still comparable; otherwise ``None`` and a stated reason.
    comparable = now["identity_available"] and before["identity_available"]
    deltas: dict[str, int | None] = {
        "reviewable_bytes": now["reviewable_bytes"] - before["reviewable_bytes"],
        "duplicate_groups": (
            now["duplicate_groups"] - before["duplicate_groups"] if comparable else None
        ),
    }
    return {
        "title": "Changes",
        "identity": identity,
        "previous": {"scan_run_id": int(previous["id"]), "completed_at": previous["completed_at"]},
        "unchanged": buckets.get("UNCHANGED", {"count": 0, "bytes": 0}),
        "buckets": {status: buckets[status] for status in _CHANGE_BUCKETS if status in buckets},
        "largest": largest,
        "totals": {"current": now, "previous": before},
        "deltas": deltas,
        "duplicate_note": (
            None
            if comparable
            else (
                f"Scan #{int(previous['id'])} has no verified content identity recorded, so its "
                "duplicate groups cannot be counted and the two scans are not comparable."
            )
        ),
    }


def build_changes_context(database, config) -> dict:
    return changes_digest(database, config)


def build_coverage_context(database, config) -> dict:
    """Per-source: how much of it is verified present on another source, and what is not.

    One source (or none) cannot be covered by anything, which the template states rather than
    rendering a table of zeroes that reads like "nothing is backed up".
    """
    from ..coverage import coverage, source_roots

    sources = source_roots(database)
    return {
        "title": "Cross-drive coverage",
        "sources": [
            {**source, **coverage(database, source["id"], limit=20)} for source in sources
        ],
        "comparable": len(sources) > 1,
    }


CONTEXT_BUILDERS = {
    "summary": build_summary_context,
    "changes": build_changes_context,
    "coverage": build_coverage_context,
    "inventory": build_inventory_context,
    "exact_duplicates": build_duplicates_context,
    "directory_overlap": build_directory_overlap_context,
    "document_versions": build_document_versions_context,
    "image_groups": build_image_groups_context,
    "large_files": build_large_files_context,
    "projects": build_projects_context,
    "errors": build_errors_context,
}
