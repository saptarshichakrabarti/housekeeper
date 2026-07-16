# drive_housekeeper implementation tracker

This tracker maps the original upgrade prompt to the repository state. Checkmarks mean implemented and covered by automated validation; partial items remain unchecked until the full requested behavior and tests exist.

## Completed foundation

- [x] Additive SQLite migrations, content objects, content links, artifacts, compressed text blobs, sources, relationships, review sessions/history, and graph cache.
- [x] Incremental scans with source registration, stat reuse, change records, source association, durable scan jobs, cancellation, and source-root foreign keys.
- [x] Verified duplicate grouping, conservative classifications, manifest-only movement, restore verification, and no delete/purge command.
- [x] Local FastAPI dashboard with vendored HTMX and Cytoscape.js, CSP/CSRF/read-only controls, bounded APIs, graph limits, and manifest export snapshots.
- [x] Synthetic benchmark scripts, migration/maintenance commands, optional Rust hashing boundary, lint/type/test coverage.

## Pipeline and jobs

- [x] Create durable jobs for every long operation: duplicate/overlap analysis, classification, reports, manifest export/validation, move, restore, maintenance, and graph work.
- [ ] Provide signal-driven pause/resume, durable checkpoints, and partial-operation recovery for every job type.
- [ ] Use a single database writer with bounded batching/backpressure for all large ingestion and analysis writes.
- [ ] Apply process isolation/resource limits consistently to document, image, archive, and media parsing on every supported platform.
- [ ] Persist measured storage-profile selection and apply it to scan/hash/parser workers throughout.

## Incremental inventory

- [ ] Record and test every scan state: `UNCHANGED`, `METADATA_CHANGED`, `CONTENT_POSSIBLY_CHANGED`, `MOVED_OR_RENAMED_CANDIDATE`, `NEW`, `MISSING`, and `ERROR`.
- [x] Use quick-hash candidates before full-hash confirmation for rename detection.
- [x] Reuse verified links and artifacts automatically after confirmed renames.
- [x] Add reappearing-file, unstable-file, mount-change, and source-association integration tests.

## Analyzer quality and scope

- [ ] Complete scope support for scan ID, MIME, unique/duplicate candidates, full age range, and every analyzer. (Shared scopes now cover the CLI analyzers and content registry; broad integration coverage and scope-aware report/graph entry points remain.)
- [ ] Add representative-path fallback tests for all analyzers.
- [ ] Replace quadratic document/image/directory/backup candidate loops with scalable candidate funnels.
- [ ] Add thumbnails/contact sheets, robust video metadata, nested archive protection, and richer structured document/version metadata. (Thumbnail artifacts, `ffprobe` metadata, archive nesting protection, and structured document fields are present; contact-sheet UI/tests remain.)
- [x] Persist first-class document-family and image-group entities rather than only pairwise relationships.

## Dashboard

- [ ] Replace inline page construction with templates, typed view models, services, and reusable HTMX components.
- [x] Complete overview charts and freshness-aware summary cards.
- [ ] Complete review filtering, detail drawers, decisions, safe bulk actions, and pagination controls. (Filters, decision APIs, guarded bulk actions, cursor pagination, and entry/duplicate HTMX detail fragments are complete; reusable decision forms remain.)
- [ ] Complete duplicate, backup, document, image, project, jobs, and manifest-center detail workflows.
- [ ] Add dashboard integration/security coverage for every page, filter, state change, and malformed input.

## Graph

- [ ] Implement rich backup/project/document/image/selected-directory projections and all typed node/edge families.
- [ ] Add multi-level aggregation, type filters, explorer links, saved projections, SVG export, and persisted layout positions.
- [ ] Add graph projection/cache/evidence tests at aggregate scale.

## Database, review, benchmarks, Rust, and docs

- [ ] Add major-version migration fixtures/recovery tests and complete freshness-aware materialized summaries.
- [ ] Remove remaining inventory-scale unbounded queries and add query-plan regression tests.
- [ ] Bind CSV and JSONL decision manifests to snapshots with complete policy/analysis/canonical evidence.
- [ ] Add the full small/medium/large benchmark suite with recorded comparable results and CI algorithmic guards.
- [ ] Complete acceleration protocol progress/cancellation/equivalence tests.
- [ ] Finish the complete documentation and acceptance-test matrix from the original prompt.
