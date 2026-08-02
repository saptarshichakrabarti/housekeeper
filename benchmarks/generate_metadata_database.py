"""Create a synthetic SQLite metadata corpus without creating a million filesystem files.

The point is to reach *plan-quality* scale — the size at which SQLite's query planner and B-tree
depths behave as they will in production — without paying for that many real files on disk. At the
top end (``--entries 100000000``) this writes a multi-tens-of-GB database; that is the corpus the
scale probes below need, and it is why ``--profile-only`` exists to read one back without rebuilding.

``--snapshots N`` writes the same logical tree as N complete scan runs, deduplicating content across
them exactly as the scanner would, so the set-based rescan diff can be measured against it
(``benchmarks/soak_rescan.py``). ``--profile-only`` prints per-table and per-index byte usage from
the ``dbstat`` virtual table, which is what decides the page-size and partial-index questions the
performance plan leaves open — measured, not argued.
"""

import argparse
from pathlib import Path

from housekeeper.database import Database


def _insert_snapshot(db: Database, run: int, source_id: int, entries: int, batch_size: int) -> None:
    conn = db.connect()
    for start in range(0, entries, batch_size):
        end = min(entries, start + batch_size)
        conn.executemany(
            "INSERT INTO filesystem_entries(scan_run_id,source_root_id,source_root,absolute_path,"
            "relative_path,name,suffix,entry_type,size_bytes,device_id,inode_or_file_id,modified_at,"
            "scan_status) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            [
                (
                    run,
                    source_id,
                    "/synthetic",
                    f"/synthetic/{i // 1000:05d}/item-{i:09d}.dat",
                    f"{i // 1000:05d}/item-{i:09d}.dat",
                    f"item-{i:09d}.dat",
                    ".dat",
                    "file",
                    i % 8192,
                    64,  # one synthetic device
                    100_000 + i,  # a stable inode per logical path, so an unchanged rescan matches
                    1_700_000_000.0 + i,
                    "OK",
                )
                for i in range(start, end)
            ],
        )
        conn.commit()
        print({"snapshot_run": run, "inserted": end, "total": entries}, flush=True)


def _profile(db: Database) -> None:
    """Print per-object byte usage. Uses ``dbstat`` when the build has it, else the file total."""
    conn = db.connect()
    print(f"database_bytes: {db.path.stat().st_size}")
    try:
        rows = conn.execute(
            "SELECT name, SUM(pgsize) AS bytes, COUNT(*) AS pages FROM dbstat GROUP BY name ORDER BY bytes DESC"
        ).fetchall()
    except Exception as exc:  # noqa: BLE001 - dbstat is a compile-time option; degrade cleanly
        print(f"dbstat unavailable ({exc}); showing file total only")
        return
    print(f"{'object':<48}{'bytes':>16}{'pages':>12}")
    for row in rows:
        print(f"{str(row[0]):<48}{int(row[1]):>16}{int(row[2]):>12}")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--entries", type=int, default=1_000_000)
    parser.add_argument("--batch-size", type=int, default=10_000)
    parser.add_argument(
        "--snapshots",
        type=int,
        default=1,
        help="number of complete scan runs of the same tree (for rescan-diff soak tests)",
    )
    parser.add_argument(
        "--profile-only",
        action="store_true",
        help="do not build; read an existing corpus and print per-object byte usage",
    )
    args = parser.parse_args()

    db = Database(args.database)
    db.initialize()
    if args.profile_only:
        _profile(db)
        return

    conn = db.connect()
    source_id = conn.execute(
        "INSERT INTO source_roots(display_name,source_fingerprint,last_mount_path) "
        "VALUES('synthetic','synthetic-v1','/synthetic') RETURNING id"
    ).fetchone()[0]
    runs = []
    for snapshot in range(args.snapshots):
        run = db.create_scan_run("/synthetic", "synthetic-v1", "benchmark")
        _insert_snapshot(db, run, source_id, args.entries, args.batch_size)
        conn.execute(
            "UPDATE scan_runs SET status='COMPLETE',completed_at=CURRENT_TIMESTAMP WHERE id=?", (run,)
        )
        runs.append(run)
    # Deduplicate content across snapshots by logical path, exactly as the scanner leaves it.
    conn.execute(
        """INSERT OR IGNORE INTO content_objects(hash_algorithm,full_hash,size_bytes)
           SELECT DISTINCT 'sha256',printf('%064x',size_bytes*31+(inode_or_file_id%100000)),size_bytes
           FROM filesystem_entries WHERE entry_type='file'"""
    )
    conn.execute(
        """INSERT OR IGNORE INTO entry_content_links(entry_id,content_object_id,link_status,size_verified,hash_verified)
           SELECT e.id,co.id,'VERIFIED',1,1 FROM filesystem_entries e
           JOIN content_objects co ON co.full_hash=printf('%064x',e.size_bytes*31+(e.inode_or_file_id%100000))
             AND co.size_bytes=e.size_bytes
           WHERE e.entry_type='file'"""
    )
    conn.execute(
        "UPDATE source_roots SET latest_complete_scan_run_id=? WHERE id=?", (runs[-1], source_id)
    )
    db.refresh_current_inventory_views()
    conn.commit()
    conn.execute("ANALYZE")
    conn.commit()
    print({"runs": runs, "source_id": source_id})
    print(db.database_stats(check_integrity=False))


if __name__ == "__main__":
    main()
