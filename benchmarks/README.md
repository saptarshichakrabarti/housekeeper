# Benchmarks

Benchmarks are intentionally local and synthetic. `benchmark_scan.py` measures traversal of a generated fixture; database and graph measurements should use copied databases, never mounted external drives. `generate_metadata_database.py --entries 1000000` produces a million-entry metadata corpus without generating a million real files, then `benchmark_database.py`, `benchmark_dashboard_queries.py`, and `benchmark_graph_projection.py` exercise bounded queries.

Record Python version, platform, storage profile, entry count, database/WAL sizes, commit, and command line with each run. CI smoke tests should assert query shape (keyset pagination and bounded graph limits); performance gates should compare against an explicitly recorded baseline on the same runner and fail only on an agreed relative regression, not a fixed wall-clock value.
