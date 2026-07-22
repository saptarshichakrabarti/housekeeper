"""Exact duplicate analysis using a size → verified-content funnel without large lists."""

from pathlib import Path

from ..config import AppConfig
from ..database import Database
from ..hashing import compute_full_hash
from ..relationships import invalidate_relationships, upsert_relationship
from ..jobs import check_cancelled, update_job
from .scope import AnalyzerScope, scoped_entry_ids

ANALYZER_NAME = "exact_duplicates"
ANALYZER_VERSION = "3"


def _ensure_candidate_links(
    database: Database,
    config: AppConfig,
    job_id: int | None = None,
    allowed_entry_ids: set[int] | None = None,
) -> None:
    """Hash only sizes with more than one occurrence when no verified link exists yet."""
    if job_id:
        # Mirrors the loop's own predicate below, so the denominator matches exactly what will be
        # processed. Scope (``allowed_entry_ids``) is a Python-side post-filter, not part of this
        # SQL, so a scoped run's total is an upper bound — the bar never over-reports completion.
        total = database.fetch_one(
            """SELECT COUNT(*) AS n FROM filesystem_entries e
               LEFT JOIN entry_content_links l ON l.entry_id=e.id AND l.link_status='VERIFIED'
               WHERE e.entry_type='file' AND l.entry_id IS NULL AND e.size_bytes IN
               (SELECT size_bytes FROM filesystem_entries WHERE entry_type='file' GROUP BY size_bytes HAVING COUNT(*)>1)"""
        )
        update_job(database, job_id, total_estimate=int(total["n"]) if total else 0)
    sizes = database.iter_rows(
        "SELECT size_bytes FROM filesystem_entries WHERE entry_type='file' GROUP BY size_bytes HAVING COUNT(*)>1"
    )
    processed = 0
    for size_row in sizes:
        for row in database.iter_rows(
            """SELECT e.id,e.scan_run_id,e.absolute_path,e.size_bytes FROM filesystem_entries e
               LEFT JOIN entry_content_links l ON l.entry_id=e.id AND l.link_status='VERIFIED'
               WHERE e.entry_type='file' AND e.size_bytes=? AND l.entry_id IS NULL ORDER BY e.id""",
            (size_row["size_bytes"],),
        ):
            if allowed_entry_ids is not None and int(row["id"]) not in allowed_entry_ids:
                continue
            if job_id:
                check_cancelled(database, job_id)
            result = compute_full_hash(
                Path(row["absolute_path"]),
                config.section("hashing")["algorithm"],
                config.section("hashing")["full_hash_block_bytes"],
            )
            database.connect().execute(
                "INSERT OR REPLACE INTO file_signatures(entry_id,full_hash,hash_algorithm,hash_status,hash_error,full_hash_computed_at) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)",
                (
                    row["id"],
                    result.digest,
                    config.section("hashing")["algorithm"],
                    "OK" if result.stable else "ERROR",
                    result.error,
                ),
            )
            if result.stable and result.digest:
                content_id = database.get_or_create_content_object(
                    config.section("hashing")["algorithm"],
                    result.digest,
                    result.size,
                    row["scan_run_id"],
                )
                database.link_entry_content(row["id"], content_id, "")
            processed += 1
            if job_id and processed % 100 == 0:
                update_job(
                    database,
                    job_id,
                    "RUNNING",
                    processed_count=processed,
                    current_item=str(row["absolute_path"]),
                    checkpoint={"phase": "candidate-hashing", "last_entry_id": int(row["id"])},
                )
    database.connect().commit()


def run_exact_duplicate_analysis(
    database: Database,
    config: AppConfig,
    job_id: int | None = None,
    scope: AnalyzerScope | None = None,
) -> None:
    """Create duplicate groups directly from verified content-object links."""
    allowed_entry_ids = scoped_entry_ids(database, scope) if scope else None
    _ensure_candidate_links(database, config, job_id, allowed_entry_ids)
    conn = database.connect()
    conn.execute(
        "UPDATE review_decisions SET stale=1,updated_at=CURRENT_TIMESTAMP WHERE target_type='DUPLICATE_GROUP' AND current=1"
    )
    invalidate_relationships(database, relationship_type="EXACT_DUPLICATE_MEMBER")
    conn.execute("DELETE FROM exact_duplicate_members")
    conn.execute("DELETE FROM exact_duplicate_groups")
    if job_id:
        # Revises the same job's total_estimate for this second phase (candidate hashing above has
        # a different, larger denominator) — update_job supports changing it mid-run.
        group_total = database.fetch_one(
            """SELECT COUNT(*) AS n FROM (
                 SELECT co.id FROM content_objects co JOIN entry_content_links l ON l.content_object_id=co.id
                 JOIN filesystem_entries e ON e.id=l.entry_id
                 WHERE l.link_status='VERIFIED' AND l.hash_verified=1 AND e.entry_type='file'
                 GROUP BY co.id,co.full_hash,co.size_bytes HAVING COUNT(*)>1)"""
        )
        update_job(database, job_id, total_estimate=int(group_total["n"]) if group_total else 0)
    groups = database.iter_rows(
        """SELECT co.id AS content_id,co.full_hash,co.size_bytes,COUNT(*) AS member_count
           FROM content_objects co JOIN entry_content_links l ON l.content_object_id=co.id
           JOIN filesystem_entries e ON e.id=l.entry_id
           WHERE l.link_status='VERIFIED' AND l.hash_verified=1 AND e.entry_type='file'
           GROUP BY co.id,co.full_hash,co.size_bytes HAVING COUNT(*)>1 ORDER BY co.id"""
    )
    completed = 0
    for group in groups:
        if job_id:
            check_cancelled(database, job_id)
        members = database.fetch_all(
            """SELECT e.id,e.absolute_path FROM entry_content_links l JOIN filesystem_entries e ON e.id=l.entry_id
               WHERE l.content_object_id=? AND l.link_status='VERIFIED' AND e.entry_type='file'
               ORDER BY length(e.absolute_path),lower(e.absolute_path),e.id""",
            (group["content_id"],),
        )
        if allowed_entry_ids is not None:
            members = [member for member in members if int(member["id"]) in allowed_entry_ids]
        if len(members) < 2:
            continue
        canonical = members[0]
        cursor = conn.execute(
            "INSERT INTO exact_duplicate_groups(full_hash,size_bytes,member_count,canonical_entry_id,canonical_selection_reason,verified) VALUES(?,?,?,?,?,1)",
            (
                group["full_hash"],
                group["size_bytes"],
                len(members),
                canonical["id"],
                "verified content identity; shortest path",
            ),
        )
        assert cursor.lastrowid is not None
        group_id = int(cursor.lastrowid)
        conn.executemany(
            "INSERT INTO exact_duplicate_members(group_id,entry_id,is_canonical,readable) VALUES(?,?,?,1)",
            [(group_id, member["id"], int(member["id"] == canonical["id"])) for member in members],
        )
        upsert_relationship(
            database,
            "DUPLICATE_GROUP",
            group_id,
            "CONTENT_OBJECT",
            int(group["content_id"]),
            "EXACT_DUPLICATE_MEMBER",
            1.0,
            {"member_count": len(members), "size_bytes": int(group["size_bytes"])},
            "2",
        )
        for member in database.iter_rows(
            "SELECT entry_id FROM exact_duplicate_members WHERE group_id=?", (group_id,)
        ):
            upsert_relationship(
                database,
                "DUPLICATE_GROUP",
                group_id,
                "ENTRY",
                int(member["entry_id"]),
                "EXACT_DUPLICATE_MEMBER",
                1.0,
                {"canonical": int(member["entry_id"]) == int(canonical["id"])},
                "2",
            )
        completed += 1
        if job_id:
            update_job(
                database,
                job_id,
                "RUNNING",
                processed_count=completed,
                success_count=completed,
                checkpoint={"phase": "groups", "last_content_object_id": int(group["content_id"])},
            )
    conn.commit()
