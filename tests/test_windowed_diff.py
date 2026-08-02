"""The scan epilogue windowed: same result whatever the window size, and idempotent on re-run.

The set-based diff is the codebase's most load-bearing optimisation. Windowing it must not change
what it computes — only the transaction size — so these tests pin the change classification against
a tiny window (many chunks) and against a single window (one chunk), and prove the INSERT OR IGNORE
behind the new unique index makes a re-executed window a no-op.
"""

from __future__ import annotations

import housekeeper.scanner as scanner_module
from housekeeper.scanner import DriveScanner


def _two_snapshots(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir(parents=True)
    for i in range(40):
        (root / f"f{i:03d}.txt").write_text(f"body {i}\n")
    scanner = DriveScanner(database, config)
    scanner.scan(root, incremental=False)
    # Mutate: change some, delete some, add some — every change class represented.
    for i in range(0, 40, 5):
        (root / f"f{i:03d}.txt").write_text(f"CHANGED body {i} with more text\n")
    for i in range(1, 40, 11):
        (root / f"f{i:03d}.txt").unlink()
    for i in range(100, 105):
        (root / f"f{i:03d}.txt").write_text(f"new file {i}\n")
    scanner.scan(root, resume=False, incremental=True)
    return root


def _change_counts(database):
    latest = database.fetch_one("SELECT MAX(id) AS id FROM scan_runs")["id"]
    return {
        row["change_status"]: row["n"]
        for row in database.fetch_all(
            "SELECT change_status, COUNT(*) AS n FROM scan_entry_changes WHERE scan_run_id=? GROUP BY change_status",
            (latest,),
        )
    }


def test_unique_index_exists(database):
    assert database.fetch_one(
        "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_changes_run_entry'"
    ), "the (scan_run_id, entry_id) unique index must back INSERT OR IGNORE"


def test_tiny_windows_match_a_single_window(config, database, tmp_path, monkeypatch):
    """One row per window must classify exactly as one window for the whole run."""
    monkeypatch.setattr(scanner_module, "SET_OP_CHUNK_ROWS", 1)
    _two_snapshots(config, database, tmp_path)
    tiny = _change_counts(database)

    # A second workspace, identical tree and mutations, with a single large window.
    from housekeeper.config import load_config
    from housekeeper.database import Database

    monkeypatch.setattr(scanner_module, "SET_OP_CHUNK_ROWS", 10_000_000)
    big_config = load_config(workspace_override=tmp_path / "ws2")
    big_db = Database(big_config.database_path)
    big_db.initialize()
    try:
        _two_snapshots(big_config, big_db, tmp_path / "b")
        big = _change_counts(big_db)
    finally:
        big_db.close()

    assert tiny == big, f"window size changed the diff: {tiny} vs {big}"
    # And the classification is the one we constructed: some changed, some missing, some new.
    assert big.get("CONTENT_POSSIBLY_CHANGED", 0) >= 1
    assert big.get("MISSING", 0) >= 1
    assert big.get("NEW", 0) >= 5


def test_re_running_the_diff_is_idempotent(config, database, tmp_path):
    """A resumed run may re-execute a window; INSERT OR IGNORE must not duplicate change rows."""
    _two_snapshots(config, database, tmp_path)
    latest = database.fetch_one("SELECT MAX(id) AS id FROM scan_runs")["id"]
    previous = database.fetch_one(
        "SELECT id FROM scan_runs WHERE id<>? ORDER BY id DESC LIMIT 1", (latest,)
    )["id"]
    before = database.fetch_one(
        "SELECT COUNT(*) AS n FROM scan_entry_changes WHERE scan_run_id=?", (latest,)
    )["n"]

    # Re-run the change diff for the same run: every INSERT OR IGNORE hits the unique index.
    DriveScanner(database, config)._record_changes(latest, previous, force_rehash=False)
    after = database.fetch_one(
        "SELECT COUNT(*) AS n FROM scan_entry_changes WHERE scan_run_id=?", (latest,)
    )["n"]
    assert after == before, "re-running the diff duplicated change rows"
