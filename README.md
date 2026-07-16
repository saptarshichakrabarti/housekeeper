# drive_housekeeper

`drive_housekeeper` creates a factual, resumable SQLite inventory of a messy backup drive and produces conservative review recommendations. It never permanently deletes files. The only mutating operation is moving individually approved manifest rows into an external review folder; every move is hashed, recorded, and restorable.

Scanning is read-only, does not follow symbolic links by default, never executes content, and treats errors and unsupported formats conservatively. Exact duplicates require verified SHA-256 hashes and retain a deterministic canonical copy. Similarity is never proof of disposability. Movement requires an explicit edited manifest, revalidates size and hash immediately beforehand, refuses collisions and nested source/review roots, verifies the destination, then removes the source only after a verified copy. There is no delete or purge command.

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
```

Edit `approved` to `true` only after manual review. Keep the database and review root off the source drive, and back up the database before long runs. Reports are static HTML containing paths and metadata, so protect the workspace. The current foundation includes scanner, streamed stable hashing, exact duplicate analysis, baseline policies, manifests, verified movement/restore safeguards, and report skeletons. Rich archive/document/image/project analyzers are intentionally conservative follow-up stages; there is no OCR, extraction, macro execution, embedding, video comparison, or directory-wide movement.

Run `pytest`, `ruff check .`, and `mypy src`. Python 3.11+ is supported on Linux, macOS, and Windows where filesystem permissions and mount behavior allow it. Estimated reviewable bytes are not guaranteed reclaimable space.
