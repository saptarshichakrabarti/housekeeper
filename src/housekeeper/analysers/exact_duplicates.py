"""Exact duplicate analysis using a size → verified-content funnel without large lists."""

from collections.abc import Iterable, Mapping
from typing import Any, cast

from ..config import AppConfig
from ..core.identity import ensure_content_identity, stream_identity_candidates
from ..database import Database
from ..jobs import check_cancelled, checkpoint, update_job
from ..relationships import invalidate_relationships, upsert_relationship
from .scope import AnalyserScope, resolve_scope

ANALYSER_NAME = "exact_duplicates"
ANALYSER_VERSION = "3"


def _ensure_candidate_links(
    database: Database,
    config: AppConfig,
    scope: AnalyserScope,
    job_id: int | None = None,
) -> None:
    """Hash only sizes with more than one occurrence when no verified link exists yet.

    The size funnel is scoped. Unscoped, a file and its own snapshot from the previous scan share a
    size, so every re-scanned file entered the funnel and the "hash only duplicate candidates"
    optimisation degenerated towards hashing the entire drive, every run.
    """
    entry_sql, scope_params = scope.entry_id_sql()
    candidates = f"""FROM filesystem_entries e
        LEFT JOIN entry_content_links l ON l.entry_id=e.id AND l.link_status='VERIFIED'
        WHERE e.entry_type='file' AND l.entry_id IS NULL AND e.id IN ({entry_sql})"""
    if job_id:
        # Mirrors the loop's own predicate exactly, so the progress denominator is the real one.
        total = database.fetch_one(
            f"""SELECT COUNT(*) AS n {candidates} AND e.size_bytes IN
                (SELECT size_bytes FROM filesystem_entries WHERE entry_type='file'
                 AND id IN ({entry_sql}) GROUP BY size_bytes HAVING COUNT(*)>1)""",
            (*scope_params, *scope_params),
        )
        # Named, because this job runs two phases with two denominators and the progress counter
        # restarts between them — without the label that reads as the stage running twice.
        update_job(
            database,
            job_id,
            total_estimate=int(total["n"]) if total else 0,
            current_item="hashing candidates",
        )
    # The whole funnel as one stream, hashed by the shared parallel identity service rather than one
    # file at a time on this thread — this used to run *before* content analysis, so on a first scan
    # its serial loop did the bulk of the byte volume while the worker pool sat idle. Ordered so
    # inode-mates are adjacent, which is what lets the service read a hard-linked backup copy once.
    # Streamed on a read-only connection while the service writes on the writer connection.
    stream = stream_identity_candidates(
        database.reader(),
        f"""SELECT e.id,e.scan_run_id,e.absolute_path,e.size_bytes,e.device_id,e.inode_or_file_id,e.nlink
            {candidates} AND e.size_bytes IN
              (SELECT size_bytes FROM filesystem_entries WHERE entry_type='file'
               AND id IN ({entry_sql}) GROUP BY size_bytes HAVING COUNT(*)>1){{keyset}}""",
        (*scope_params, *scope_params),
    )
    ensure_content_identity(
        database,
        config,
        # sqlite3.Row is a mapping at runtime (the service reads it by key); typeshed does not model
        # that, so the stream is cast to the documented contract rather than materialised to dicts.
        cast("Iterable[Mapping[str, Any]]", stream),
        job_id,
        record_errors=True,
        progress_phase="hashing candidates",
    )


def run_exact_duplicate_analysis(
    database: Database,
    config: AppConfig,
    job_id: int | None = None,
    scope: AnalyserScope | None = None,
) -> None:
    """Create duplicate groups directly from verified content-object links."""
    scope = resolve_scope(database, scope)
    entry_sql, scope_params = scope.entry_id_sql()
    _ensure_candidate_links(database, config, scope, job_id)
    conn = database.connect()
    conn.execute(
        "UPDATE review_decisions SET stale=1,updated_at=CURRENT_TIMESTAMP WHERE target_type='DUPLICATE_GROUP' AND current=1"
    )
    invalidate_relationships(database, relationship_type="EXACT_DUPLICATE_MEMBER")
    # A group's identity is the content object it groups, so groups are upserted in place below
    # rather than deleted and reinserted. Deleting them reallocated ids, which violated the
    # canonical_overrides foreign key — one recorded override broke every later run, permanently —
    # and threw away 181k delete/insert pairs' worth of work per run for no change. Stable ids also
    # mean a user's canonical choice stays attached, so it is honoured here.
    overrides = {
        int(row["content_object_id"]): int(row["canonical_entry_id"])
        for row in database.fetch_all(
            """SELECT g.content_object_id,o.canonical_entry_id FROM canonical_overrides o
               JOIN exact_duplicate_groups g ON g.id=o.duplicate_group_id
               WHERE g.content_object_id IS NOT NULL"""
        )
    }
    if job_id:
        # Revises the same job's total_estimate for this second phase (candidate hashing above has
        # a different, larger denominator) — update_job supports changing it mid-run.
        group_total = database.fetch_one(
            f"""SELECT COUNT(*) AS n FROM (
                 SELECT co.id FROM content_objects co JOIN entry_content_links l ON l.content_object_id=co.id
                 JOIN filesystem_entries e ON e.id=l.entry_id
                 WHERE l.link_status='VERIFIED' AND l.hash_verified=1 AND e.entry_type='file'
                 AND e.id IN ({entry_sql})
                 GROUP BY co.id,co.full_hash,co.size_bytes HAVING COUNT(*)>1)""",
            scope_params,
        )
        update_job(
            database,
            job_id,
            processed_count=0,
            total_estimate=int(group_total["n"]) if group_total else 0,
            current_item="grouping duplicates",
        )
    # Two copies means two copies *in the same snapshot*. Counting across scans made a re-scanned
    # unique file a duplicate of its own previous self — the G2 violation this scoping exists for.
    #
    # Ordered by digest, not by co.id: content-object ids are allocated as hash results arrive from
    # a thread pool, so ordering by id made the traversal depend on thread scheduling. Identical
    # bytes must always produce identical grouping and canonical choices.
    groups = database.iter_rows(
        f"""SELECT co.id AS content_id,co.full_hash,co.size_bytes,COUNT(*) AS member_count
           FROM content_objects co JOIN entry_content_links l ON l.content_object_id=co.id
           JOIN filesystem_entries e ON e.id=l.entry_id
           WHERE l.link_status='VERIFIED' AND l.hash_verified=1 AND e.entry_type='file'
           AND e.id IN ({entry_sql})
           GROUP BY co.id,co.full_hash,co.size_bytes HAVING COUNT(*)>1
           ORDER BY co.full_hash,co.size_bytes""",
        scope_params,
    )
    completed = 0
    for group in groups:
        if job_id:
            check_cancelled(database, job_id)
        members = database.fetch_all(
            f"""SELECT e.id,e.absolute_path FROM entry_content_links l JOIN filesystem_entries e ON e.id=l.entry_id
               WHERE l.content_object_id=? AND l.link_status='VERIFIED' AND e.entry_type='file'
               AND e.id IN ({entry_sql})
               ORDER BY length(e.absolute_path),lower(e.absolute_path),e.id""",
            (group["content_id"], *scope_params),
        )
        if len(members) < 2:
            continue
        content_id = int(group["content_id"])
        overridden = overrides.get(content_id)
        canonical = next((m for m in members if int(m["id"]) == overridden), members[0])
        reason = (
            "user canonical override"
            if overridden is not None and int(canonical["id"]) == overridden
            else "verified content identity; shortest path"
        )
        row = conn.execute(
            """INSERT INTO exact_duplicate_groups(content_object_id,full_hash,size_bytes,member_count,canonical_entry_id,canonical_selection_reason,verified)
               VALUES(?,?,?,?,?,?,1)
               ON CONFLICT(content_object_id) WHERE content_object_id IS NOT NULL
               DO UPDATE SET member_count=excluded.member_count,
                 canonical_entry_id=excluded.canonical_entry_id,
                 canonical_selection_reason=excluded.canonical_selection_reason,verified=1
               RETURNING id""",
            (
                content_id,
                group["full_hash"],
                group["size_bytes"],
                len(members),
                canonical["id"],
                reason,
            ),
        ).fetchone()
        assert row is not None
        group_id = int(row[0])
        conn.execute("DELETE FROM exact_duplicate_members WHERE group_id=?", (group_id,))
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
        checkpoint(
            database,
            job_id,
            processed_count=completed,
            state={"phase": "groups", "last_content_object_id": int(group["content_id"])},
        )
    # Retire groups this run no longer stands behind, in two set-based passes:
    #   * content that is not duplicated anywhere, by any measure — pure garbage collection;
    #   * content this scope owns but no longer sees two copies of, which is how a group created
    #     by an older unscoped run (a file paired with its own prior snapshot) gets cleaned up.
    # Groups whose content lies entirely outside this scope belong to another source and are left
    # alone, and a group carrying a user's canonical override is always kept — silently discarding
    # a recorded decision is worse than keeping the row that explains it.
    content_sql, content_params = scope.content_object_id_sql()
    duplicated_globally = """SELECT l.content_object_id FROM entry_content_links l
        JOIN filesystem_entries e ON e.id=l.entry_id
        WHERE l.link_status='VERIFIED' AND l.hash_verified=1 AND e.entry_type='file'
        GROUP BY l.content_object_id HAVING COUNT(*)>1"""
    duplicated_in_scope = f"""SELECT l.content_object_id FROM entry_content_links l
        JOIN filesystem_entries e ON e.id=l.entry_id
        WHERE l.link_status='VERIFIED' AND l.hash_verified=1 AND e.entry_type='file'
        AND e.id IN ({entry_sql})
        GROUP BY l.content_object_id HAVING COUNT(*)>1"""
    conn.execute(
        f"""DELETE FROM exact_duplicate_groups
            WHERE id NOT IN (SELECT duplicate_group_id FROM canonical_overrides)
              AND (content_object_id IS NULL
                   OR content_object_id NOT IN ({duplicated_globally})
                   OR (content_object_id IN ({content_sql})
                       AND content_object_id NOT IN ({duplicated_in_scope})))""",
        (*content_params, *scope_params),
    )
    conn.commit()
