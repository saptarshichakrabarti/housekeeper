# Dashboard

Install `pip install -e '.[dashboard]'`, then run `housekeeper dashboard`. It binds to loopback by default and serves local HTML, an escaped review table, overview JSON, and bounded graph JSON. The dashboard vendors HTMX 2.0.4 and Cytoscape.js locally; it has no Node.js or runtime CDN requirement. The graph view uses Cytoscape.js with concentric, breadth-first, grid, and cose layouts, search, node/edge evidence detail, progressive expansion, and PNG export.

Dashboard actions are not movement actions. Manifest export creates a review snapshot and downloads JSONL; save it locally, validate it, perform a dry run, then run the separate explicit CLI movement command. Non-loopback binding requires explicit configuration and should be protected outside trusted local use. Runtime assets are local and no telemetry is used.
