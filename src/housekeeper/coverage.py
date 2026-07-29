"""Is this drive's content present somewhere else? — the question asked before retiring a backup.

For every file of one source, does its content object also hang off a *current* entry of another
source? Content identity is global in this database, so the answer is a join rather than a new
analysis. Three buckets, and the third is the safety-critical one:

* **verified elsewhere** — same content, verified by hash, on at least one other source;
* **only copy here** — verified identity, and no other current source has it;
* **unknown** — no verified hash at all, so *nothing* is claimed. An unhashed file is never counted
  as covered.

The wording is deliberate: "verified present elsewhere", never "safe to delete". This module reads;
moving files remains the separate, explicit, manifest-verified flow.
"""

from __future__ import annotations

BUCKETS = ("covered", "unique", "unknown")

_STATE_SQL = """
    CASE WHEN l.entry_id IS NULL THEN 'unknown'
         WHEN EXISTS(SELECT 1 FROM entry_content_links l2
                     JOIN current_entries o ON o.id=l2.entry_id
                     WHERE l2.content_object_id=l.content_object_id
                       AND o.source_root_id<>e.source_root_id{against})
         THEN 'covered' ELSE 'unique' END
"""


def _against_clause(against: list[int] | None) -> tuple[str, dict[str, int]]:
    """Named parameters, not positional ones.

    The ``against`` placeholders sit inside a CASE expression that appears in the SELECT list of one
    query and in the WHERE clause of another — i.e. at different positions relative to the other
    parameters. Binding them by name is what makes the two queries agree; with ``?`` they silently
    did not, and the bucket counts stayed right while the file list was drawn from the wrong source.
    """
    if not against:
        return "", {}
    names = [f"against{index}" for index, _ in enumerate(against)]
    clause = " AND o.source_root_id IN (" + ",".join(f":{name}" for name in names) + ")"
    return clause, dict(zip(names, (int(value) for value in against), strict=True))


def source_roots(database) -> list[dict]:
    """Sources with a current snapshot — the only ones coverage can be computed for or against."""
    return [
        {"id": int(row["id"]), "name": str(row["display_name"]), "path": str(row["last_mount_path"])}
        for row in database.fetch_all(
            "SELECT id,display_name,last_mount_path FROM source_roots "
            "WHERE latest_complete_scan_run_id IS NOT NULL ORDER BY id"
        )
    ]


def coverage(database, source_root_id: int, against: list[int] | None = None, limit: int = 100) -> dict:
    """Bucket counts and bytes for one source, plus its largest files that exist nowhere else."""
    clause, extra = _against_clause(against)
    state = _STATE_SQL.format(against=clause)
    rows = database.fetch_all(
        f"""SELECT {state} state, COUNT(*) n, COALESCE(SUM(e.size_bytes),0) b
            FROM current_entries e
            LEFT JOIN entry_content_links l ON l.entry_id=e.id AND l.link_status='VERIFIED'
            WHERE e.source_root_id=:source AND e.entry_type='file'
            GROUP BY state""",
        {"source": int(source_root_id), **extra},
    )
    buckets = {
        bucket: {"count": 0, "bytes": 0} for bucket in BUCKETS
    } | {
        str(row["state"]): {"count": int(row["n"]), "bytes": int(row["b"])} for row in rows
    }
    unique_files = [
        {"relative_path": str(row["relative_path"]), "size_bytes": int(row["size_bytes"] or 0)}
        for row in database.fetch_all(
            f"""SELECT e.relative_path,e.size_bytes FROM current_entries e
                LEFT JOIN entry_content_links l ON l.entry_id=e.id AND l.link_status='VERIFIED'
                WHERE e.source_root_id=:source AND e.entry_type='file' AND ({state})='unique'
                ORDER BY e.size_bytes DESC LIMIT :limit""",
            {"source": int(source_root_id), "limit": int(limit), **extra},
        )
    ]
    total = sum(bucket["count"] for bucket in buckets.values())
    return {
        "source_root_id": int(source_root_id),
        "against": list(against or []),
        "total_files": total,
        "buckets": buckets,
        "unique_files": unique_files,
        "summary": (
            f"{buckets['covered']['count']:,} of {total:,} files verified elsewhere · "
            f"{buckets['unique']['count']:,} only copy here · "
            f"{buckets['unknown']['count']:,} unknown (no verified hash)"
        ),
    }
