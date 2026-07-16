from ..relationships import upsert_relationship
from .scope import AnalyzerScope, scoped_entry_ids
from ..jobs import check_cancelled, update_job


def run_backup_lineage_analysis(
    database, config, scope: AnalyzerScope | None = None, job_id: int | None = None
):
    rows = database.fetch_all(
        "SELECT entry_id,recursive_file_count,recursive_size_bytes FROM directory_summaries WHERE recursive_file_count>0"
    )
    allowed = scoped_entry_ids(database, scope, "directory") if scope else None
    if allowed is not None:
        rows = [row for row in rows if int(row["entry_id"]) in allowed]
    for index, left in enumerate(rows, start=1):
        if job_id:
            check_cancelled(database, job_id)
        left_hashes = {
            r["full_hash"]
            for r in database.fetch_all(
                "SELECT s.full_hash FROM filesystem_entries e JOIN file_signatures s ON s.entry_id=e.id WHERE e.parent_entry_id=? AND s.full_hash IS NOT NULL",
                (left["entry_id"],),
            )
        }
        if not left_hashes:
            continue
        for right in rows:
            if left["entry_id"] == right["entry_id"] or left["entry_id"] > right["entry_id"]:
                continue
            right_hashes = {
                r["full_hash"]
                for r in database.fetch_all(
                    "SELECT s.full_hash FROM filesystem_entries e JOIN file_signatures s ON s.entry_id=e.id WHERE e.parent_entry_id=? AND s.full_hash IS NOT NULL",
                    (right["entry_id"],),
                )
            }
            shared = len(left_hashes & right_hashes)
            confidence = shared / max(1, min(len(left_hashes), len(right_hashes)))
            if confidence >= 0.9:
                upsert_relationship(
                    database,
                    "DIRECTORY",
                    left["entry_id"],
                    "DIRECTORY",
                    right["entry_id"],
                    "LIKELY_BACKUP_SUCCESSOR",
                    confidence,
                    {
                        "shared_hashes": shared,
                        "left_hashes": len(left_hashes),
                        "right_hashes": len(right_hashes),
                    },
                    "1",
                )
        if job_id:
            update_job(
                database,
                job_id,
                "RUNNING",
                processed_count=index,
                checkpoint={"last_directory_id": int(left["entry_id"])},
            )
