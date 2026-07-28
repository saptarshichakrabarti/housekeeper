# Schema

Schema v4 retains every v1 table and adds source roots, content objects, entry-content links, analysis artifacts, text blobs, scan changes, durable jobs, review sessions/decisions/history/snapshots, typed relationships, graph layout cache, projects, canonical overrides, migration progress, and materialized summaries. Verified full hashes are deduplicated by algorithm, digest, and size; links explicitly record verification and source stat state.

Current inventory is exposed through a relational `current_*` view layer rather than by deleting
history. The scan pointer selects current entries and classifications; dependent views derive current
content, artifacts, duplicate groups and members, projects, relationships and groups, overlaps,
collections, record-series assignments, and preservation assessments. User-facing reports,
dashboard/API queries, and materialized summaries must read this layer so deleted snapshots remain
auditable without being presented as current state.

`schema_migrations` records applied versions. `migration_progress` makes the large v4 cursor migration recoverable. Before an upgrade, `Database.initialize()` checks integrity and never removes legacy rows; `housekeeper database migrate --dry-run` reports current/target versions, estimated affected entries, temporary-space estimate, and backup recommendation. Important lookups are indexed by scan/path, content, artifact, job, review, relationship source/target, and scan change state.
