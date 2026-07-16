# Threat model

The local dashboard is an input surface, so filenames and paths are escaped, identifiers are typed, graph requests are bounded, and arbitrary SQL/path access is absent. Movement remains separate and manifest/hash validated. Stale decisions are marked rather than silently applied. Migration uses SQLite transactions and backups. An optional acceleration subprocess must be capability-checked and protocol-validated; Python fallback is fail-closed.
