"""Backup lineage: which directories are likely successors/predecessors of one another.

Uses the same build-once hash-set map + inverted-index candidate generation as
``directory_overlap`` so unrelated directories are never compared (no O(n²) per-pair scans).
"""

from ..jobs import check_cancelled, update_job
from ..relationships import upsert_relationship
from .directory_overlap import generate_candidate_directory_pairs
from .scope import AnalyserScope, resolve_scope


def _direct_child_hashes(database, entry_id: int) -> set[str]:
    return {
        r["full_hash"]
        for r in database.iter_rows(
            "SELECT s.full_hash FROM filesystem_entries e JOIN file_signatures s ON s.entry_id=e.id "
            "WHERE e.parent_entry_id=? AND s.full_hash IS NOT NULL",
            (entry_id,),
        )
    }


def run_backup_lineage_analysis(
    database, config, scope: AnalyserScope | None = None, job_id: int | None = None
) -> None:
    entry_sql, params = resolve_scope(database, scope).entry_id_sql("directory")
    rows = database.fetch_all(
        "SELECT entry_id FROM directory_summaries "
        f"WHERE recursive_file_count>0 AND entry_id IN ({entry_sql})",
        params,
    )
    dir_hashes: dict[int, set[str]] = {}
    for index, row in enumerate(rows, start=1):
        entry_id = int(row["entry_id"])
        if job_id and index % 100 == 0:
            check_cancelled(database, job_id)
        hashes = _direct_child_hashes(database, entry_id)
        if hashes:
            dir_hashes[entry_id] = hashes
    for a_id, b_id in sorted(generate_candidate_directory_pairs(dir_hashes)):
        left, right = dir_hashes[a_id], dir_hashes[b_id]
        shared = len(left & right)
        confidence = shared / max(1, min(len(left), len(right)))
        if confidence >= 0.9:
            upsert_relationship(
                database,
                "DIRECTORY",
                a_id,
                "DIRECTORY",
                b_id,
                "LIKELY_BACKUP_SUCCESSOR",
                confidence,
                {"shared_hashes": shared, "left_hashes": len(left), "right_hashes": len(right)},
                "1",
            )
    # This analyser is a stage: the write primitives no longer commit per row, so the one
    # commit that makes its work durable belongs here.
    database.connect().commit()
    if job_id:
        update_job(database, job_id, "RUNNING", processed_count=len(dir_hashes))
