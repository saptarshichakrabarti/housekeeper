# Rust boundary

Keep configuration, migrations, policy, decisions, dashboard, reports, graph semantics, and manifest semantics in Python. Protocol v1 defines `capabilities`, `scan`, `quick_hash`, `full_hash`, `aggregate_directories`, `verify_manifest`, and `copy_and_verify`, with structured result/error records and optional progress events. The Python backend implements the safe reference behavior; the optional `rust/housekeeper-core` subprocess currently accelerates only stable full/quick hashing and reports that capability precisely. Python detects capabilities, validates protocol version, and falls back safely for every unsupported operation.

No Rust rewrite is justified yet: the recorded synthetic bottlenecks are inventory/database orchestration and parser behavior, where Python remains the control plane. Any future Rust operation must first pass equivalence tests for hashes, stat normalization, errors, manifest verification, and progress semantics.
