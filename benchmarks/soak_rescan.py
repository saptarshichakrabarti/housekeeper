"""Soak set-based rescan diff cost without creating real files.

Stages an unchanged rescan in SQL, times scanner epilogue with work counters. Pair with
``generate_metadata_database.py``. Shape-checked by ``tests/test_corpus_shapes.py``.
"""

import argparse
import time
from pathlib import Path

from housekeeper.config import load_config
from housekeeper.core import counters
from housekeeper.database import Database
from housekeeper.scanner import DriveScanner


def _current_run(db: Database) -> tuple[int, str]:
    row = db.fetch_one(
        "SELECT id, source_root_fingerprint AS fp FROM scan_runs WHERE status='COMPLETE' ORDER BY id DESC LIMIT 1"
    )
    if not row:
        raise SystemExit("corpus has no COMPLETE scan run; build one with generate_metadata_database.py")
    return int(row["id"]), str(row["fp"])


def _stage_unchanged_rescan(db: Database, previous_id: int, fingerprint: str) -> int:
    """Insert a new run whose entries copy the previous snapshot verbatim — an unchanged rescan."""
    run = db.create_scan_run("/synthetic", fingerprint, "soak")
    conn = db.connect()
    conn.execute(
        """INSERT INTO filesystem_entries(scan_run_id,source_root_id,source_root,absolute_path,
             relative_path,name,suffix,entry_type,size_bytes,device_id,inode_or_file_id,modified_at,scan_status)
           SELECT ?,source_root_id,source_root,absolute_path,relative_path,name,suffix,entry_type,
             size_bytes,device_id,inode_or_file_id,modified_at,scan_status
           FROM filesystem_entries WHERE scan_run_id=?""",
        (run, previous_id),
    )
    conn.commit()
    return run


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("--rescans", type=int, default=5)
    args = parser.parse_args()

    db = Database(args.database)
    db.initialize()
    config = load_config(workspace_override=args.database.parent)
    scanner = DriveScanner(db, config)

    print(f"{'rescan':>7}{'entries':>12}{'statements':>12}{'commits':>10}{'seconds':>10}{'wal_peak_mb':>12}")
    previous_id, fingerprint = _current_run(db)
    entries = int(db.fetch_one("SELECT COUNT(*) n FROM filesystem_entries WHERE scan_run_id=?", (previous_id,))["n"])
    for iteration in range(args.rescans):
        run = _stage_unchanged_rescan(db, previous_id, fingerprint)
        started = time.perf_counter()
        with counters.recording() as counted:
            scanner._link_parents(run)
            scanner._record_changes(run, previous_id, force_rehash=False)
            counters.record_max("wal_bytes_stage_end", db.wal_bytes())
        db.execute("UPDATE scan_runs SET status='COMPLETE',completed_at=CURRENT_TIMESTAMP WHERE id=?", (run,))
        db.connect().commit()
        elapsed = time.perf_counter() - started
        # Settle the WAL exactly as a real scan does at its end (optimize_after_write), so the peak
        # reported is this rescan's own, not an accumulation the production path never carries.
        db.checkpoint_wal("TRUNCATE")
        print(
            f"{iteration:>7}{entries:>12}{int(counted['sql_statements']):>12}"
            f"{int(counted['commits']):>10}{elapsed:>10.3f}{counted['wal_bytes_stage_end'] / 1e6:>12.1f}"
        )
        previous_id = run
    db.close()


if __name__ == "__main__":
    main()
