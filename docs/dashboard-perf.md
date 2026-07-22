# Dashboard responsiveness on a large inventory

Goal: keep the dashboard responsive on a multi-GB / ~1.5M-entry SQLite inventory without ever
moving data or writing from a read surface.

## What changed

1. **Independent read-only connection pool.** Every dashboard read (the service and all GET
   endpoints) runs on a per-thread, `mode=ro`, `query_only=ON` connection
   (`Database.reader()` / `Database._read_conn()`). WAL already allows many readers + one writer,
   so overview aggregates, the review page, and the 3s jobs poll no longer serialize against each
   other or the background runner's writes. Only the writer connection records decisions, runs
   control operations, and reconciles jobs.
2. **Tuned read connections.** Each read connection sets `mmap_size=256MB`, `cache_size=-65536`
   (64MB), `temp_store=MEMORY`. After a large write job (`Database.optimize_after_write`) the DB
   runs `PRAGMA optimize` + `wal_checkpoint(TRUNCATE)`, and `ANALYZE` once after the first scan so
   the planner has statistics and the WAL a scan leaves behind never slows later reads.
3. **Materialized overview.** The overview is served from `materialized_summaries`
   (metric counts + the five chart aggregates), refreshed at the end of every scan/analyze and via
   a CSRF-guarded **Refresh now** button. A 45s in-process TTL cache sits on top. The three
   `database_stats` COUNTs are folded in too, so a normal load counts nothing live.
4. **Hot-path indexes** (`idx_review_decisions_target`, `idx_dupe_members_entry`,
   `idx_entries_type_suffix_size`, `idx_entries_suffix`) — see
   `tests/test_dashboard_indexes.py` for the before/after `EXPLAIN QUERY PLAN`.
5. **Idle-poll suspension.** The jobs fragment keeps its 3s cadence only while a job is active;
   when idle it re-arms on a `job-started` event alone (the control endpoints send
   `HX-Trigger: job-started`), so an idle dashboard issues no repeating job queries.

## Cold-load overview benchmark

Synthetic inventory built by `scripts/benchmark_overview.py` (no real drive). Measured on this
machine (Python 3.12, local SSD):

| Inventory | Path | Queries per load | Wall time |
|-----------|------|------------------|-----------|
| 500,000 entries / 206 MB | **before** (live full-table scans) | 14 | **679 ms** |
| 500,000 entries / 206 MB | **after** (materialized summaries) | 4 | **~0.02 ms** |

The "before" path runs 9 metric COUNT/SUM scans + 5 chart `GROUP BY` scans over the inventory on
*every* page load; cost grows with the row count. The "after" path issues 3 primary-key lookups
against `materialized_summaries` plus one indexed `jobs` count — bounded regardless of inventory
size. Extrapolating linearly, a 1.5M-entry / ~1.9 GB inventory is ≈3× the "before" scan cost
(~2 s per load) while the "after" cost stays flat. The one-time refresh that recomputes the
summaries is paid at the end of a scan/analyze (or on explicit **Refresh now**), off the page-load
path.

Reproduce: `python scripts/benchmark_overview.py [N]` (default N=300,000).

## Acceptance checks (tests)

- Read pool is independent, read-only, thread-local — `tests/test_dashboard_readpool.py`
- Overview issues no full-table scan on a normal load; refresh recomputes — `tests/test_dashboard_overview_materialized.py`
- New indexes are chosen by the planner (before/after query plans) — `tests/test_dashboard_indexes.py`
- Idle jobs poll suspends and resumes on job start — `tests/test_dashboard_idle_poll.py`
