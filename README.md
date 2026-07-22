# drive_housekeeper

`drive_housekeeper` creates a factual, resumable SQLite inventory of a messy backup drive and produces conservative review recommendations. It never permanently deletes files. The only mutating operation is moving individually approved manifest rows into an external review folder; every move is hashed, recorded, and restorable.

Scanning is read-only, does not follow symbolic links by default, never executes content, and treats errors and unsupported formats conservatively. Exact duplicates require verified SHA-256 hashes and retain a deterministic canonical copy. Similarity is never proof of disposability. Movement requires an explicit edited manifest, revalidates size and hash immediately beforehand, refuses collisions and nested source/review roots, verifies the destination, then removes the source only after a verified copy. There is no delete or purge command.

## Quickstart (one command)

The fastest way to point the tool at a drive and get a full picture. This is entirely read-only —
it scans, analyzes, classifies, and writes reports; it never moves or deletes anything.

```bash
make install                       # create .venv and install with all optional features
make quickstart SOURCE=/mnt/drive  # scan + analyze + classify + reports, then print a summary
make dashboard                     # browse results locally (loopback only)
```

Without `make`, the same thing:

```bash
pip install -e '.[analysis,dashboard]'
housekeeper quickstart /mnt/drive        # add --no-reports to skip HTML, --json for machine output
housekeeper dashboard
```

`quickstart` runs every step inside a durable, pausable job and is safe to re-run — each run reports
the current snapshot of the drive. It deliberately stops short of any movement: staging approved
files into a review folder remains the separate, explicit, manifest-verified flow shown below.
Run `make help` to see all targets (`install-dev`, `check`, `test`, `lint`, `typecheck`,
`benchmark`, `clean`).

## Install and synthetic workflow

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,analysis]"
housekeeper init-workspace
python scripts/create_test_fixture.py --output /tmp/housekeeper-fixture --clean
housekeeper scan /tmp/housekeeper-fixture
housekeeper analyze exact-duplicates
housekeeper classify
housekeeper report all
housekeeper export-review --output workspace/manifests/review_candidates.csv
housekeeper validate-manifest workspace/manifests/review_candidates.csv
housekeeper move-to-review workspace/manifests/review_candidates.csv /tmp/drive-review --dry-run
housekeeper move-to-review workspace/manifests/review_candidates.csv /tmp/drive-review --yes
```

Edit `approved` to `true` only after manual review. Keep the database and review root off the source drive, and back up the database before long runs. `--yes` is required for an actual move or restore; destination collisions are refused. Reports are static HTML containing paths and metadata, so protect the workspace. Optional archive/document/image/media/project analyzers are bounded and conservative: no macros are executed, no archives are extracted, no OCR or embeddings are run, and similarity never becomes an automatic movement recommendation.

Run `pytest`, `ruff check .`, and `mypy src`. Python 3.11+ is supported on Linux, macOS, and Windows where filesystem permissions and mount behavior allow it. Estimated reviewable bytes are not guaranteed reclaimable space.

## Second-generation platform

Known sources are scanned incrementally by default. Verified full hashes are deduplicated into content objects, and analyzer artifacts are reused only when content identity, analyzer version, and configuration fingerprint match.

```bash
housekeeper sources list
housekeeper scan /path/to/source --incremental --changed-only
housekeeper diff 1 2
housekeeper analyze all --changed-only
housekeeper jobs list
housekeeper database migrate --dry-run
housekeeper database backup workspace/backups/inventory.sqlite
housekeeper graph build universe
housekeeper benchmark scan /tmp/synthetic-fixture
```

Create a persistent review session, record decisions, and export an immutable, hash-backed manifest. The dashboard can record decisions and inspect manifest status but cannot move files:

```bash
housekeeper review create "April review"
housekeeper review decision SESSION_ID ENTRY ENTRY_ID MARK_KEEP
housekeeper review export SESSION_ID --output workspace/manifests/april.jsonl --format jsonl
housekeeper review validate SESSION_ID
housekeeper validate-manifest workspace/manifests/april.jsonl
housekeeper dashboard --no-open-browser
```

Install the optional local dashboard with `pip install -e '.[dashboard]'`. It binds to loopback, serves local assets only, escapes filenames, enforces bounded pagination/graph requests, and requires CSRF tokens for state-changing decisions. It never exposes arbitrary SQL, paths, file contents, or movement endpoints. See `docs/architecture.md`, `docs/dashboard.md`, `docs/graph_model.md`, and `docs/performance.md`.

The dashboard graph is rendered solely with the vendored Cytoscape.js distribution at `/static/vendor/cytoscape.min.js`; no CDN or Node.js runtime is required. Local declarative fragment refreshes keep job tables current without exposing a remote dependency. The optional analysis extra enables conservative DOCX, XLSX, PPTX, PDF, image, and archive metadata/text extraction. Parser failures and unavailable optional parsers remain protected artifacts.
