# Architecture

The scanner records source-root-relative filesystem entries and source identity independent of mount path. Verified bytes become reusable `content_objects`; `entry_content_links` represents each path occurrence. Versioned `analysis_artifacts` and compressed content text blobs are keyed by content identity, analyser version, and configuration fingerprint. Relationships are typed, versioned, confidence-scored, and evidence-bearing. Policies produce classifications; review sessions persist decisions, stale state, immutable history, canonical overrides, and snapshots. The local FastAPI dashboard provides overview, review, duplicate, backup, document, image, project, jobs, graph, and manifest views; CLI movement remains the safety boundary. Graph projections aggregate relationships and enforce node/edge limits. Python remains the control plane with an optional JSONL acceleration boundary.

## Snapshots and the current inventory

A scan is a **snapshot**, not an update. Every row in `filesystem_entries` belongs to exactly one
`scan_runs` row, and rescanning a drive adds a snapshot rather than mutating the previous one — that
retention is the product: a superseded snapshot's verdict is the audit trail.

The consequence is that "every row in `filesystem_entries`" is not the drive. It is the drive plus
every earlier version of itself, so an unscoped query relates a file to its own prior snapshot and can
conclude the current copy is a removable duplicate. That is guardrail **G2**, and it has been violated
more than once — first by an analyser, later independently by reports, exports, review manifests, the
dashboard overview and the review queue, each of which had to remember a predicate and did not.

So scoping is a property of the relation you name, not a parameter you pass:

| Relation | Contents |
|---|---|
| `current_entries` | `filesystem_entries` restricted to the latest COMPLETE run of each source root |
| `current_classifications` | `classifications` for those entries |
| `filesystem_entries`, `classifications` | every snapshot ever recorded |

**Current-state output reads the views. History is the explicit choice.** For analysers the same rule
is expressed as `AnalyserScope`: `scope=None` resolves to the current inventory, and
`AnalyserScope.all_history()` is how you ask for the other thing.

Two implementation details matter if you touch this:

- The views carry their run ids as **literals**, refreshed in the same transaction that moves
  `source_roots.latest_complete_scan_run_id`. The natural alternative —
  `scan_run_id IN (SELECT latest_complete_scan_run_id FROM source_roots …)` — plans as
  `SCAN filesystem_entries` with a list subquery and post-filters, measured 4–15× slower than the
  literal form on a 20-snapshot corpus and *slower than not scoping at all*. A writer that marks a run
  COMPLETE outside `DriveScanner` must call `Database.refresh_current_inventory_views()`.
- **By-id lookups stay on the base table.** Resolving an entry id a human already chose — a manifest
  row, a detail page — must not fail because the drive was rescanned since. Scoping those would turn
  "the drive changed under you" into "missing entry"; the drift and hash checks in the movement path
  are what decide whether the operation is still safe.

Content-level relations are deliberately *not* snapshot-scoped. `content_objects`,
`analysis_artifacts`, `relationships`, `content_relationships` and the graph are keyed by content
identity, which is what makes a file recognisable across drives and across rescans.
