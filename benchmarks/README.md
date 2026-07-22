# Benchmarks

Benchmarks are intentionally local and synthetic. `benchmark_scan.py` measures traversal of a generated fixture; database and graph measurements should use copied databases, never mounted external drives. `generate_metadata_database.py --entries 1000000` produces a million-entry metadata corpus without generating a million real files, then `benchmark_database.py`, `benchmark_dashboard_queries.py`, and `benchmark_graph_projection.py` exercise bounded queries.

Record Python version, platform, storage profile, entry count, database/WAL sizes, commit, and command line with each run. CI smoke tests should assert query shape (keyset pagination and bounded graph limits); performance gates should compare against an explicitly recorded baseline on the same runner and fail only on an agreed relative regression, not a fixed wall-clock value.

## Recorded baseline and regression comparison

`baseline.json` is a committed, machine-readable baseline produced by `src/housekeeper/benchmarking.py` over three deterministic profiles (`small`/`medium`/`large`). It separates two kinds of measurement so the guard is meaningful across machines:

- **Counts** (files, directories, content objects, exact-duplicate groups) are deterministic and platform-independent. Any drift is treated as a *correctness* regression and always fails a comparison.
- **Timings** are stamped with the recording machine's environment fingerprint (Python version, implementation, system, machine). They are compared only when the current runner matches that fingerprint; on a different runner the timing check is reported as *skipped*, never failed.

Commands:

- `housekeeper benchmark baseline [--baseline PATH]` — run the suite and write/refresh the baseline artifact (defaults to `benchmarks/baseline.json`). Regenerate this whenever the deterministic corpus or the recorded metrics intentionally change.
- `housekeeper benchmark compare [--baseline PATH] [--tolerance F]` — run the suite and diff it against the baseline. Exits non-zero on any count drift, or on a same-runner timing regression beyond `--tolerance` (default `0.5`, i.e. 50% slower). Prints the full diff as JSON.

The suite always runs against a throwaway temporary workspace, so it never touches a real inventory database. `tests/test_benchmark_baseline.py` asserts the counts are reproducible and that the committed baseline still matches a fresh run.
