"""Create a synthetic SQLite metadata corpus without creating a million filesystem files."""

import argparse
from pathlib import Path

from housekeeper.database import Database

parser = argparse.ArgumentParser()
parser.add_argument("database", type=Path)
parser.add_argument("--entries", type=int, default=1_000_000)
parser.add_argument("--batch-size", type=int, default=10_000)
args = parser.parse_args()

db = Database(args.database)
db.initialize()
run = db.create_scan_run("/synthetic", "synthetic-v1", "benchmark")
conn = db.connect()
for start in range(0, args.entries, args.batch_size):
    end = min(args.entries, start + args.batch_size)
    conn.executemany(
        "INSERT INTO filesystem_entries(scan_run_id,source_root,absolute_path,relative_path,name,suffix,entry_type,size_bytes,scan_status) VALUES(?,?,?,?,?,?,?,?,?)",
        [(run, "/synthetic", f"/synthetic/{i // 1000:05d}/item-{i:09d}.dat", f"{i // 1000:05d}/item-{i:09d}.dat", f"item-{i:09d}.dat", ".dat", "file", i % 8192, "OK") for i in range(start, end)],
    )
    conn.commit()
    print({"inserted": end, "total": args.entries}, flush=True)
conn.execute("UPDATE scan_runs SET status='COMPLETE',completed_at=CURRENT_TIMESTAMP WHERE id=?", (run,))
conn.commit()
db.refresh_materialized_summaries(run)
print(db.database_stats())
