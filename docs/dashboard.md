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
