# Performance

The pipeline uses streamed hashing, size candidate funnels, SQLite WAL, content-link reuse, bounded worker queues, isolated parser timeouts, keyset-style dashboard pagination, set-based scan diffing, and graph limits. Incremental scans reuse verified links when the prior stat fingerprint is unchanged and record change evidence. Materialized summaries keep repeated dashboard overview reads away from raw inventory tables.

## Recorded synthetic baseline

On 2026-07-16, the current development environment scanned the generated 10,000-entry fixture in **0.7516 seconds** (100,000 logical bytes; no directory hierarchy). This is a local synthetic metadata/I/O smoke measurement, not a portability promise. Reproduce it with:

```bash
PYTHONPATH=src python benchmarks/generate_large_fixture.py /tmp/housekeeper-benchmark-fixture --files 10000
PYTHONPATH=src python benchmarks/benchmark_scan.py /tmp/housekeeper-benchmark-fixture
```

`generate_metadata_database.py --entries 1000000` creates the large simulated metadata corpus. Run database, dashboard, and graph probes against that database; compare results only with a baseline collected on the same host/storage profile. CI tests query shape and hard limits rather than fragile absolute timing.
