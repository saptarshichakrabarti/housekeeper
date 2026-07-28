# Threat model

The local dashboard is an input surface, so filenames and paths are escaped, identifiers are typed, graph requests are bounded, and arbitrary SQL/path access is absent. Movement remains separate and manifest/hash validated. Stale decisions are marked rather than silently applied. Migration uses SQLite transactions and backups. An optional acceleration subprocess must be capability-checked and protocol-validated; Python fallback is fail-closed.

## Report and export output

Reports are static HTML and the recommendation exports are CSV/JSONL. All four are written to the
workspace and are expected to be read, copied and shared — that is what they are for. They therefore
carry two kinds of sensitive content, treated differently:

**Source-relative paths and filenames are in scope by design.** A review tool that hides what it is
recommending is useless. They are HTML-escaped at render, never interpolated into SQL, and never
executed.

**Absolute paths are not needed for reading, and leak more than the drive.** A path like
`/Users/alice/Pictures/2019/img.jpg` carries an account name and a machine's directory layout, and it
survives being pasted into a ticket. `reporting.redact_source_paths` replaces the mount path with
`<source>` in reports and exports, keeping the source-relative path and identifying the drive by its
fingerprint instead. It is off by default: an operator triaging their own drive wants a path they can
paste into a terminal, and a redaction nobody asked for would be a usability tax that encourages
turning it off wholesale.

**Review manifests are deliberately excluded from redaction.** A manifest is the movement contract:
`move-to-review` revalidates every row by absolute path and by hash immediately before touching a
file. A redacted manifest would fail that check, or worse invite someone to paste a guessed path back
in. Redaction protects a document meant for reading; the manifest is a document meant for executing.
Keep the workspace and its manifests under the same protection as the database.

## Retained history

Scan history is retained by default, so the workspace accumulates one snapshot of the inventory per
scan — including paths that have since been deleted from the drive. That is intentional (the audit
trail is the product), but it means the database is a longer-lived record than the drive itself.
`housekeeper database prune-snapshots` bounds it explicitly; it refuses to remove any snapshot a
review decision, session baseline or canonical override still references, so bounding history cannot
silently destroy evidence of a decision.
