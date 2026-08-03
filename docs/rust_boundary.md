# Rust boundary

Keep configuration, migrations, policy, decisions, dashboard, reports, graph semantics, and manifest semantics in Python. Protocol v1 defines `capabilities`, `scan`, `quick_hash`, `full_hash`, `identity_hash`, `aggregate_directories`, `verify_manifest`, and `copy_and_verify`, with structured result/error records and optional progress events. When `housekeeper-core` is installed, normal hashing selects it by default; `identity_hash` produces full and sampled digests from one sequential read. Python validates capabilities and falls back to its reference implementation when the binary is missing, incompatible, or fails.

No Rust rewrite is justified yet: the recorded synthetic bottlenecks are inventory/database orchestration and parser behavior, where Python remains the control plane. Any future Rust operation must first pass equivalence tests for hashes, stat normalization, errors, manifest verification, and progress semantics.
