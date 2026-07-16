# Upgrade audit

Date: 2026-07-16

## Baseline

- Python: 3.11 or newer (`pyproject.toml`); the current environment runs the test suite successfully.
- Packaging: setuptools with a `housekeeper` console entry point; runtime dependencies are PyYAML and Jinja2.
- Test command: `pytest -q` (baseline: 3 passed).
- Lint command: `ruff check .` (baseline: 6 pre-existing unused-import/unused-variable findings).
- Type-check command: `mypy src` (baseline: 6 pre-existing errors in database, archives, scanner, and reporting).
- Schema version: 1 (`SCHEMA_VERSION = 1`); the existing database layer creates tables directly and has no ordered migration framework.
- CLI commands: `init-workspace`, `scan`, `scan-status`, `stats`, `analyze`, `classify`, `report`, `export-review`, `validate-manifest`, `move-to-review`, and `restore`.

## Current architecture

The package is a compact Python application under `src/housekeeper`. `scanner.py` traverses a source root with `os.scandir`, stores one `filesystem_entries` row per scan/path, and checkpoints aggregate counters. `hashing.py` provides streamed quick and full hashing with a stability check. Analyzer modules operate directly on filesystem-entry rows; exact duplicates write hashes and duplicate groups. `policies.py` classifies files conservatively, while `reporting.py` emits small static HTML reports.

Review is a file-based workflow: `manifests.py` exports CSV rows, `validate-manifest` checks schema and basic database drift, and `review_mover.py` performs an explicit, hash-verified copy into a non-nested review root before unlinking the source. `restore.py` verifies the review copy and restores only when safe. No permanent delete or purge command exists.

## Current schema

The schema consists of `schema_migrations`, `scan_runs`, `filesystem_entries`, `file_signatures`, `classifications`, `analysis_jobs`, `exact_duplicate_groups`, `exact_duplicate_members`, `directory_summaries`, and `move_transactions`. Hashes and analysis are entry/path-centric. Existing indexes cover scan id, relative path, size, full hash, and classification. WAL mode and foreign keys are enabled on the primary connection.

## Public interfaces and compatibility boundaries

- `housekeeper.cli:main` and the existing command names are public operational interfaces.
- `Database`, `DriveScanner`, `compute_full_hash`, manifest loaders/validators, mover, restore, policy classification, and report generation are imported directly by tests/scripts and must remain usable.
- Existing CSV review manifests and JSONL transaction logs must remain readable.
- The safety boundary is the explicit approved manifest; dashboard or decision features must feed snapshot/export validation and must not directly move files.

## Reusable modules

The scanner, streamed hashing, exact duplicate analyzer, policy engine, manifest validation, mover, restore, configuration merge, and path safety helpers are useful foundations. The current analyzer package provides conservative extension points for archives, documents, images, media, projects, versions, and directory overlap.

## Technical debt and performance assumptions

- `Database.initialize` is schema creation plus a version marker, not migration.
- `filesystem_entries` uses `INSERT OR REPLACE`, which is unsuitable for preserving historical identity and can replace rows unexpectedly.
- The scanner rescans every path and only resumes incomplete runs; repeated completed scans do not reuse hashes or analysis.
- Exact duplicate hashing is per entry, not per content object.
- Several queries use unbounded `fetchall`; there is no keyset pagination or unified job/checkpoint model.
- Dashboard, graph, source registry, persistent review history, profiling, and acceleration abstractions are absent.
- Lint/type-check debt is present before the upgrade and should be reduced without weakening safety tests.

## Migration risks

The principal risk is preserving old entry/signature rows while introducing source identities, content objects, links, and versioned analysis artifacts. Migrations must be transactional where feasible, batched for large databases, retain legacy columns/tables, and be tested against copied v1 databases. Historical scan rows must not be rewritten in a way that changes manifest validation semantics.

## Requested features already implemented

Inventory scanning, SQLite persistence, incomplete-scan resume, streamed hashing, exact duplicate grouping, baseline classification, static reports, CSV manifests, verified review movement, transaction logging, restoration, and core path safety already exist in partial form. The upgrade should extend these capabilities rather than duplicate or replace them wholesale.

## Proposed upgrade sequence

1. Add ordered migrations and safe database maintenance/backup tooling.
2. Add source roots, content objects, entry-content links, analysis artifacts, and content-level text storage; backfill verified hashes in batches.
3. Add incremental scan statuses and reuse/rename evidence while preserving the existing scan API.
4. Add durable jobs, bounded pipeline helpers, query indexes, keyset pagination, and materialized summaries.
5. Add persistent review sessions, immutable decision history, staleness, snapshots, and manifest integration.
6. Add an optional local-only dashboard that can export manifests but cannot bypass CLI movement validation.
7. Add versioned relationships and bounded graph projections with aggregation and evidence.
8. Add profiling/benchmark fixtures, Python optimizations, and a documented subprocess boundary for possible Rust acceleration.

## Safety invariants to preserve

There is no permanent deletion API. Movement remains explicit and manifest-driven, never silently overwrites destinations, verifies source and destination hashes, refuses unsafe roots, and records transactions. Parser failures, unreadable or unstable files, unknown types, stale decisions, and missing evidence remain protected/error states rather than review candidates. Paths alone are never sufficient for movement or restoration.

## Upgrade implementation status

The implementation is now at schema v4. It adds additive legacy-column migrations, a resumable migration cursor, migration-progress records, materialized summaries, WAL checkpoint/vacuum guards, read-only dashboard connections, source/device registration, reusable verified content links, scan change/rename evidence, cooperative cancellation checkpoints, immutable review snapshots, analyzer/group staleness, manifest preflight, and explicit `--yes` execution gates.

The dashboard has bounded cursor pagination, local fragment refresh, safe duplicate-group bulk decisions, jobs/progress views, structured relationship explorers, and a Cytoscape.js-only graph with cache keys tied to relationship versions, confidence filtering, progressive neighborhood expansion, and client PNG export. Synthetic benchmark tooling now includes a million-entry metadata corpus generator; the Rust boundary provides capability detection, stable full hashing, and quick hashing with Python fallback. Validation remains intentionally synthetic/copied-database only: mounted-drive performance and filesystem behavior still require deployment-specific operational qualification.
