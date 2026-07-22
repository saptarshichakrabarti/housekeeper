"""Per-report context builders. Each returns a plain dict for a Jinja template."""

from __future__ import annotations

import json

_REVIEW_CLASSES = ("REVIEW_SAFE", "REVIEW_PROBABLE", "REVIEW_BACKUP", "REVIEW_LARGE", "REVIEW_VERSION_FAMILY")


def _scan_identity(database) -> dict:
    row = database.fetch_one(
        "SELECT id,source_root,source_root_fingerprint,started_at,completed_at,status,config_hash,files_seen,directories_seen,symlinks_seen,errors_seen,bytes_seen FROM scan_runs ORDER BY id DESC LIMIT 1"
    )
    return dict(row) if row else {}


def build_summary_context(database, config) -> dict:
    identity = _scan_identity(database)
    files = database.fetch_one(
        "SELECT COUNT(*) n,COALESCE(SUM(size_bytes),0) b FROM filesystem_entries WHERE entry_type='file'"
    )
    classifications = {
        r["classification"]: r["n"]
        for r in database.fetch_all(
            "SELECT classification,COUNT(*) n FROM classifications GROUP BY classification"
        )
    }
    reviewable = {
        r["classification"]: {"count": r["n"], "bytes": r["b"]}
        for r in database.fetch_all(
            """SELECT c.classification,COUNT(*) n,COALESCE(SUM(e.size_bytes),0) b
               FROM classifications c JOIN filesystem_entries e ON e.id=c.entry_id
               WHERE c.classification LIKE 'REVIEW_%' GROUP BY c.classification"""
        )
    }
    dup = database.fetch_one(
        "SELECT COUNT(*) groups, COALESCE(SUM(member_count-1),0) redundant_members FROM exact_duplicate_groups"
    )
    dup_bytes = database.fetch_one(
        """SELECT COALESCE(SUM(g.size_bytes*(g.member_count-1)),0) b FROM exact_duplicate_groups g"""
    )
    return {
        "title": "Summary",
        "identity": identity,
        "config_fingerprint": (identity.get("config_hash") or "")[:16],
        "file_count": files["n"],
        "byte_count": files["b"],
        "unreadable": database.fetch_one(
            "SELECT COUNT(*) n FROM filesystem_entries WHERE scan_status='ERROR'"
        )["n"],
        "classifications": classifications,
        "protected": classifications.get("PROTECTED", 0),
        "unknown": classifications.get("UNKNOWN", 0),
        "errors": classifications.get("ERROR", 0),
        "exact_duplicate_groups": dup["groups"],
        "exact_duplicate_bytes": dup_bytes["b"],
        "backup_overlaps": database.fetch_one(
            "SELECT COUNT(*) n FROM relationships WHERE relationship_type='MOSTLY_CONTAINED_IN'"
        )["n"],
        "document_version_groups": database.fetch_one(
            "SELECT COUNT(*) n FROM relationship_groups WHERE group_type='DOCUMENT_FAMILY'"
        )["n"],
        "image_groups": database.fetch_one(
            "SELECT COUNT(*) n FROM relationship_groups WHERE group_type='IMAGE_SIMILARITY'"
        )["n"],
        "projects": database.fetch_one("SELECT COUNT(*) n FROM projects")["n"],
        "content_objects": database.fetch_one("SELECT COUNT(*) n FROM content_objects")["n"],
        "reviewable_by_class": reviewable,
        "content_relationships": database.fetch_all(
            "SELECT relationship_type,evidence_tier,COUNT(*) n FROM content_relationships WHERE status='ACTIVE' GROUP BY relationship_type,evidence_tier ORDER BY evidence_tier"
        ),
    }


def build_inventory_context(database, config) -> dict:
    directories = database.fetch_all(
        """SELECT source_root,
              CASE WHEN instr(relative_path,'/')=0 THEN relative_path ELSE substr(relative_path,1,instr(relative_path,'/')-1) END AS top_level,
              COUNT(*) n, COALESCE(SUM(size_bytes),0) b
           FROM filesystem_entries WHERE entry_type='file'
           GROUP BY source_root,top_level ORDER BY b DESC LIMIT 200"""
    )
    extensions = database.fetch_all(
        "SELECT COALESCE(NULLIF(suffix,''),'(none)') suffix,COUNT(*) n,COALESCE(SUM(size_bytes),0) b FROM filesystem_entries WHERE entry_type='file' GROUP BY suffix ORDER BY b DESC LIMIT 50"
    )
    return {"title": "Inventory", "directories": directories, "extensions": extensions}


def build_duplicates_context(database, config) -> dict:
    groups = []
    for group in database.iter_rows(
        "SELECT id,full_hash,size_bytes,member_count,canonical_entry_id FROM exact_duplicate_groups ORDER BY size_bytes*(member_count-1) DESC LIMIT 500"
    ):
        members = database.fetch_all(
            """SELECT e.relative_path,e.absolute_path,m.is_canonical FROM exact_duplicate_members m
               JOIN filesystem_entries e ON e.id=m.entry_id WHERE m.group_id=? ORDER BY m.is_canonical DESC""",
            (group["id"],),
        )
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
           FROM relationships r JOIN filesystem_entries a ON a.id=r.source_id JOIN filesystem_entries b ON b.id=r.target_id
           WHERE r.relationship_type='MOSTLY_CONTAINED_IN' ORDER BY r.confidence DESC LIMIT 300"""
    )
    overlaps = [{**dict(r), "evidence": json.loads(r["evidence_json"] or "{}")} for r in rows]
    return {"title": "Directory overlap", "overlaps": overlaps}


def build_document_versions_context(database, config) -> dict:
    rows = database.fetch_all(
        """SELECT a.name a_name, b.name b_name, r.confidence FROM relationships r
           JOIN filesystem_entries a ON a.id=r.source_id JOIN filesystem_entries b ON b.id=r.target_id
           WHERE r.relationship_type='LIKELY_VERSION_OF' ORDER BY r.confidence DESC LIMIT 300"""
    )
    families = database.fetch_all(
        "SELECT group_key,evidence_json FROM relationship_groups WHERE group_type='DOCUMENT_FAMILY' LIMIT 200"
    )
    return {"title": "Document versions", "pairs": rows, "families": families}


def build_image_groups_context(database, config) -> dict:
    perceptual = database.fetch_all(
        "SELECT group_key FROM relationship_groups WHERE group_type='IMAGE_SIMILARITY' LIMIT 200"
    )
    pixel = database.fetch_all(
        "SELECT COUNT(*) n FROM content_relationships WHERE relationship_type='PIXEL_IDENTICAL' AND status='ACTIVE'"
    )
    return {"title": "Image groups", "perceptual_groups": perceptual, "pixel_identical": pixel[0]["n"]}


def build_large_files_context(database, config) -> dict:
    threshold = config.section("reporting")["large_file_threshold_bytes"]
    rows = database.fetch_all(
        "SELECT relative_path,absolute_path,size_bytes FROM filesystem_entries WHERE entry_type='file' AND size_bytes>=? ORDER BY size_bytes DESC LIMIT 500",
        (threshold,),
    )
    return {"title": "Large files", "threshold": threshold, "files": rows}


def build_projects_context(database, config) -> dict:
    rows = database.fetch_all(
        "SELECT name,kind,markers_json,source_size_bytes,generated_size_bytes,environment_size_bytes,git_status FROM projects ORDER BY generated_size_bytes+environment_size_bytes DESC LIMIT 300"
    )
    projects = [{**dict(r), "markers": json.loads(r["markers_json"] or "[]")} for r in rows]
    return {"title": "Projects", "projects": projects}


def build_errors_context(database, config) -> dict:
    scan_errors = database.fetch_all(
        "SELECT relative_path,read_error FROM filesystem_entries WHERE scan_status='ERROR' LIMIT 500"
    )
    parser_errors = database.fetch_all(
        """SELECT a.analyser_name,a.error_code,a.error_message,COUNT(*) n FROM analysis_artifacts a
           WHERE a.status IN ('ERROR','UNSUPPORTED') GROUP BY a.analyser_name,a.error_code ORDER BY n DESC LIMIT 200"""
    )
    return {"title": "Errors", "scan_errors": scan_errors, "parser_errors": parser_errors}


CONTEXT_BUILDERS = {
    "summary": build_summary_context,
    "inventory": build_inventory_context,
    "exact_duplicates": build_duplicates_context,
    "directory_overlap": build_directory_overlap_context,
    "document_versions": build_document_versions_context,
    "image_groups": build_image_groups_context,
    "large_files": build_large_files_context,
    "projects": build_projects_context,
    "errors": build_errors_context,
}
