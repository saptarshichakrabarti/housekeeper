# Dashboard

Install `pip install -e '.[dashboard]'`, then run `housekeeper dashboard`. It binds to loopback by default and serves local HTML, an escaped review table, overview JSON, and bounded graph JSON. The dashboard vendors HTMX 2.0.4 and Cytoscape.js locally; it has no Node.js or runtime CDN requirement. The graph view uses Cytoscape.js with concentric, breadth-first, grid, and cose layouts, search, node/edge evidence detail, progressive expansion, and PNG export.

Dashboard actions are not movement actions. Manifest export creates a review snapshot and downloads JSONL; save it locally, validate it, perform a dry run, then run the separate explicit CLI movement command. Non-loopback binding requires explicit configuration and should be protected outside trusted local use. Runtime assets are local and no telemetry is used.

## Detail workflows

Beyond the bounded explorer tables, three GET-only detail views present richer facts (never actions):

- **Backup compare** (`/backups/<relationship id>`, linked from the Backups explorer): a side-by-side
  table of both directories in a backup/containment relationship — recursive file/directory/byte
  counts, unique-hash and internal-duplicate counts, earliest/latest modified — plus the recorded
  relationship evidence. Facts come from `directory_summaries`; nothing is recomputed on request.
- **Derivation timeline** (`/derivations/<content object id>`, linked from the Derivations
  explorer): the Tier-6 `LIKELY_*` relationships touching a content object, each resolved to
  representative file names and ordered by the derived file's modified time, with the
  modified-gap evidence surfaced. Labeled as contextual inference, never proof.
- **Image group detail** (`/images/<group id>`, linked from the Images explorer): the members of an
  `IMAGE_SIMILARITY` group with dimensions and representative paths, and the group's contact sheet
  (montage) when `housekeeper analyse contact-sheets` has rendered one. Contact-sheet JPEGs are
  served from the workspace by validated integer id only; if none exists the page says how to
  generate it.

All three respect read-only mode (they are read-only by construction) and the standard CSP.

## Space, coverage, and bulk duplicate review

- **Treemap** (`/treemap`): tile area is size on disk, tile fill is the estimated reclaimable share
  (redundant duplicate copies plus `REVIEW_*` bytes) on a single-hue light→dark ramp, with the same
  numbers repeated as a table so nothing is colour-only. Drill-down uses the same lazy one-level
  contract as the graph explorer (`/api/treemap/children`), so a million-entry drive costs the same
  as a small one. Squarify is ~50 lines in `static/treemap.js` — no charting library is vendored.
- **Coverage** (`/coverage`): per source, how many of its files have the same content, verified by
  hash, on at least one *other* source. Three buckets — verified elsewhere, only copy here, and
  unknown (no verified hash). Unhashed is never counted as covered, and the page never says "safe to
  delete".
- **Duplicate wizard** (`/wizard`): bulk review by rule — keep the canonical, keep the newest, keep
  the copy under a folder. Every rule is previewed first (keeper, proposed approvals, conflicts with
  the canonical choice, and any stale decisions from an earlier scan), and applying it writes
  ordinary `review_decisions` rows: `MARK_KEEP` for the keeper, `APPROVE_FOR_REVIEW` for the
  redundant copies. It adds **no** new mutation capability — movement remains the separate
  `export-review → validate-manifest → move-to-review` flow. The apply endpoint
  (`POST /api/review/<session>/bulk?rule=…&fingerprint=…`) re-derives the keeper server-side and never
  accepts an entry list from the client, and skips a whole group when any member is protected,
  unhashed, or unreadable. `fingerprint` is required and is the digest of the preview being confirmed
  (from `GET /api/review/<session>/bulk/preview`): if the groups changed in between — a rescan gives
  every file a new entry row — nothing is written and the current plan is re-rendered with the reason,
  so approvals can never land on entries nobody looked at. Pages are capped at 500 groups, and the
  preview's **Next** button carries the keyset cursor so a later page applies as the page it shows.

## Jobs

The Jobs page separates two levels of activity instead of listing the same work twice:

- **Runs** is the default and contains top-level operations only (`parent_job_id IS NULL`). A Quick
  start is one row, its progress is reported as completed/planned stages, and its current stage is
  named separately. Pipeline rows link to **View N stages**; standalone runs have ordinary item
  progress and no stages link.
- **Stages** is a flat, read-only history of the child work inside pipelines
  (`parent_job_id IS NOT NULL`). Each row carries full progress, results, duration, and a link back
  to its run. A run link opens this tab with `run_id` in the URL, so refresh, browser history, and
  bookmarks preserve the selection.

Both tabs filter by run id, their own type, and status. Pause, Cancel, and Resume live only on Runs:
stage control requests have always escalated to their pipeline root, so presenting them as stage
actions would misstate their scope. A stopped pipeline (`PAUSED`, `CANCELLED`, `FAILED`,
`INTERRUPTED`) offers **Resume**, which submits the same operation again. The old row stays terminal
and the new pipeline records `{"resumes": <old id>}` in its scope. A viewer dashboard (no runner)
never shows the button. The overview's **Active runs** metric likewise counts top-level operations
only and excludes paused runs.

**Pause and Cancel** are cooperative, and the request travels out-of-band: SQLite has one writer, so
a stage that is mid-transaction owns the lock and a request written only to the `jobs` row would wait
out `busy_timeout` and be lost — the click did nothing at all. The request is written to a
`job-<id>.stop` file beside the database (no lock needed) and the worker, which does own the lock,
settles the row itself at its next checkpoint; the table reads the same file, so the row shows
*stopping…* in the meantime. Measured on a 60,000-file scan: the click returns in ≤0.28 s and the run
stops ~0.3 s after it. What that latency is made of: the poll is throttled to one query per 0.25 s
per job, the table refreshes every 3 s while a job is active, and a stop only lands on a cooperative
checkpoint — per committed batch in a scan, per pair/group/object in the analysers. A single
set-based SQL statement is not interruptible, so the longest of them bounds the worst case.

How much a resume actually skips depends on the operation, and the two mechanisms are different:

- **Quickstart** skips the content-keyed stages (`content-analysis`, `document-versions`,
  `image-similarity`) that reached `COMPLETED`, matched by input fingerprint. That check rests on each
  *stage's* own terminal state, not on the pipeline's, which is exactly why an interrupted run does not
  force the next one to redo the work it already finished. The changed-only narrowing of content
  analysis is the one thing a resume switches off, because an artifact the interrupted run still owes
  is invisible in the change record.
- **The other pipelines** (analyse-all, classify, report) have no stage-level reuse: their stages are
  either keyed to entry ids (so they must re-derive) or already cheap on a second pass. The saving
  there is per-unit rather than per-stage — content artifacts, verified hashes and rendered contact
  sheets are reused whenever identity, analyser version and configuration fingerprint match — so a
  resumed analyse-all repeats the queries but not the parsing.
