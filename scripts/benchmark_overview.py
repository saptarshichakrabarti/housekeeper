"""Cold-load overview benchmark: full-scan overview vs materialized overview.

Measures query count and wall time on a synthetic inventory for the live COUNT/SUM path
versus serving ``DashboardService.overview()`` from cached summary rows.

Usage: ``python scripts/benchmark_overview.py [N]`` (default N=300_000)
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from housekeeper.dashboard.services import DashboardService
from housekeeper.database import Database

# The metric + chart SQL the old overview() ran live on every load (mirrors the pre-change code).
_LIVE_METRIC_SQL = [
    "SELECT COUNT(*) FROM source_roots",
    "SELECT COUNT(*) FROM filesystem_entries",
    "SELECT COUNT(*) FROM content_objects",
    "SELECT COUNT(*) FROM analysis_artifacts",
    "SELECT COUNT(*) FROM exact_duplicate_groups",
    "SELECT COALESCE(SUM(size_bytes),0) FROM filesystem_entries WHERE entry_type='file'",
    "SELECT COALESCE(SUM(size_bytes),0) FROM content_objects",
    "SELECT COUNT(*) FROM classifications WHERE classification='PROTECTED'",
    "SELECT COUNT(*) FROM jobs WHERE status IN ('PENDING','RUNNING','PAUSED','PAUSING','CANCELLING')",
]
# _CHART_QUERIES stores (columns, sql); the old overview ran each sql live per load.
_LIVE_CHART_SQL = [sql for _cols, sql in Database._CHART_QUERIES.values()]


def _seed(database: Database, n: int) -> None:
    c = database.connect()
    run_id = database.create_scan_run("synthetic", "fp", "cfg")
    suffixes = [".pdf", ".jpg", ".txt", ".mp4", ".docx", ".png", ".csv", ".zip"]
    tops = [f"dir{i}" for i in range(50)]
    batch = 5000
    rows = []
    for i in range(n):
        rows.append(
            (
                run_id,
                "synthetic",
                f"/src/{tops[i % len(tops)]}/file{i}{suffixes[i % len(suffixes)]}",
                f"{tops[i % len(tops)]}/file{i}{suffixes[i % len(suffixes)]}",
                f"file{i}{suffixes[i % len(suffixes)]}",
                suffixes[i % len(suffixes)],
                "file",
                (i % 100) * 1024,
            )
        )
        if len(rows) >= batch:
            c.executemany(
                "INSERT INTO filesystem_entries(scan_run_id,source_root,absolute_path,relative_path,name,suffix,entry_type,size_bytes) VALUES(?,?,?,?,?,?,?,?)",
                rows,
            )
            rows.clear()
    if rows:
        c.executemany(
            "INSERT INTO filesystem_entries(scan_run_id,source_root,absolute_path,relative_path,name,suffix,entry_type,size_bytes) VALUES(?,?,?,?,?,?,?,?)",
            rows,
        )
    # A content object + classification per ~4 entries so the joins/aggregates have real work.
    c.execute(
        "INSERT INTO content_objects(hash_algorithm,full_hash,size_bytes) SELECT 'sha256',hex(id),size_bytes FROM filesystem_entries WHERE id%4=0"
    )
    c.execute(
        "INSERT INTO classifications(entry_id,classification) SELECT id, CASE WHEN id%10=0 THEN 'PROTECTED' ELSE 'REVIEW_SAFE' END FROM filesystem_entries WHERE id%2=0"
    )
    c.commit()
    database.optimize_after_write(analyse=True)


def _time_before(database: Database) -> tuple[int, float]:
    conn = database._read_conn()
    queries = _LIVE_METRIC_SQL + _LIVE_CHART_SQL
    start = time.perf_counter()
    for sql in queries:
        conn.execute(sql).fetchall()
    return len(queries), time.perf_counter() - start


def _time_after(database: Database) -> tuple[int, float]:
    service = DashboardService(database.reader())
    executed: list[str] = []
    conn = database._read_conn()
    conn.set_trace_callback(executed.append)
    start = time.perf_counter()
    service.overview()
    elapsed = time.perf_counter() - start
    conn.set_trace_callback(None)
    return len(executed), elapsed


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 300_000
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        database = Database(Path(tmp) / "bench.sqlite")
        database.initialize()
        print(f"seeding {n:,} entries…")
        seed_start = time.perf_counter()
        _seed(database, n)
        print(f"  seeded in {time.perf_counter() - seed_start:.1f}s")

        before_q, before_t = _time_before(database)
        # Best-of-3 for the cached path since it is sub-millisecond.
        after_q, after_t = min((_time_after(database) for _ in range(3)), key=lambda r: r[1])
        size_mb = (Path(tmp) / "bench.sqlite").stat().st_size / 1e6

        print(f"\nDB size: {size_mb:.0f} MB, {n:,} entries")
        print(f"{'':10}{'queries':>10}{'wall (ms)':>14}")
        print(f"{'BEFORE':10}{before_q:>10}{before_t * 1000:>14.1f}")
        print(f"{'AFTER':10}{after_q:>10}{after_t * 1000:>14.3f}")
        print(f"speedup: {before_t / after_t:.0f}x fewer ms, {before_q - after_q} fewer queries")


if __name__ == "__main__":
    main()
