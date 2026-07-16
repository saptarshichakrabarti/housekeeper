# Optional Rust boundary

Python remains the control plane. The `housekeeper-core` subprocess implements protocol version 1 capability discovery plus stable SHA-256/SHA-512 full hashing and SHA-256 sampled quick hashing. Build with `cargo build --manifest-path rust/Cargo.toml`; select it with `HOUSEKEEPER_CORE=/path/to/housekeeper-core`. Python falls back to its own backend if the binary is absent, incompatible, or fails. Movement, traversal, and manifest execution deliberately remain in the audited Python control plane until their protocol contracts are independently tested.
