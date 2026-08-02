"""Frontier resume: an interrupted scan continues from where it stopped, not from the root.

At a billion files traversal *is* the cost, so a crash on day two must not re-pay day one. The
guard is the count of directories re-opened on resume: it must be a fraction of the tree, and the
final inventory must equal a clean scan's.
"""

from __future__ import annotations

from housekeeper.jobs import JobPaused
from housekeeper.scanner import DriveScanner


def _wide_tree(root, dirs: int, files_per_dir: int):
    root.mkdir(parents=True)
    for d in range(dirs):
        sub = root / f"dir_{d:03d}"
        sub.mkdir()
        for f in range(files_per_dir):
            (sub / f"f_{f:03d}.txt").write_text(f"{d}-{f}")
    return root


def test_resume_continues_from_the_frontier(config, database, tmp_path, monkeypatch):
    root = _wide_tree(tmp_path / "src", dirs=12, files_per_dir=10)
    # Small batches so the frontier is written often and an interruption lands mid-walk.
    config.section("scanner")["batch_size"] = 15

    # Interrupt the first scan partway by pausing after a bounded number of directories are opened.
    opened_first = {"n": 0}
    real_read = DriveScanner._read_directory

    def counting_read(directory, sort_limit):
        opened_first["n"] += 1
        if opened_first["n"] > 5:
            raise JobPaused("simulated interruption partway through the walk")
        return real_read(directory, sort_limit)

    monkeypatch.setattr(DriveScanner, "_read_directory", staticmethod(counting_read))
    scanner = DriveScanner(database, config)
    try:
        scanner.scan(root, incremental=False)
    except JobPaused:
        pass
    partial_files = database.fetch_one(
        "SELECT COUNT(*) n FROM filesystem_entries WHERE entry_type='file'"
    )["n"]
    assert partial_files < 120, "the first pass should have stopped partway"

    # Resume: count how many directories the second pass opens. It must re-open only the frontier,
    # far fewer than the whole tree, and must finish the inventory.
    opened_resume = {"n": 0}

    def counting_read_resume(directory, sort_limit):
        opened_resume["n"] += 1
        return real_read(directory, sort_limit)

    monkeypatch.setattr(DriveScanner, "_read_directory", staticmethod(counting_read_resume))
    DriveScanner(database, config).scan(root, resume=True, incremental=False)

    # The full inventory is complete — the returned counter reflects only what *this* pass walked,
    # which is the point: the directories finished before the interruption were not re-walked.
    total = database.fetch_one(
        "SELECT COUNT(*) n FROM filesystem_entries WHERE entry_type='file'"
    )["n"]
    assert total == 120, "resume must complete the inventory"
    # The tree has 13 directories (root + 12). A full re-walk would open all 13; the frontier resume
    # opens materially fewer, because the directories finished before the interruption are skipped.
    assert opened_resume["n"] < 13, f"resume re-opened {opened_resume['n']} dirs — not incremental"
    run = database.fetch_one("SELECT status FROM scan_runs ORDER BY id DESC LIMIT 1")
    assert run["status"] == "COMPLETE"


def test_resume_inventory_matches_a_clean_scan(config, database, tmp_path, monkeypatch):
    """Whatever the interruption point, the resumed inventory is identical to an uninterrupted one."""
    root = _wide_tree(tmp_path / "src", dirs=8, files_per_dir=6)
    config.section("scanner")["batch_size"] = 9

    real_read = DriveScanner._read_directory
    seen = {"n": 0}

    def interrupt_after_four(directory, sort_limit):
        seen["n"] += 1
        if seen["n"] > 4:
            raise JobPaused("stop")
        return real_read(directory, sort_limit)

    monkeypatch.setattr(DriveScanner, "_read_directory", staticmethod(interrupt_after_four))
    try:
        DriveScanner(database, config).scan(root, incremental=False)
    except JobPaused:
        pass
    monkeypatch.setattr(DriveScanner, "_read_directory", staticmethod(real_read))
    DriveScanner(database, config).scan(root, resume=True, incremental=False)
    resumed = {
        row["relative_path"]
        for row in database.fetch_all("SELECT relative_path FROM filesystem_entries")
    }

    # A clean scan of the identical tree in a second workspace.
    from housekeeper.config import load_config
    from housekeeper.database import Database

    clean_config = load_config(workspace_override=tmp_path / "ws2")
    clean_db = Database(clean_config.database_path)
    clean_db.initialize()
    try:
        DriveScanner(clean_db, clean_config).scan(root, incremental=False)
        clean = {
            row["relative_path"]
            for row in clean_db.fetch_all("SELECT relative_path FROM filesystem_entries")
        }
    finally:
        clean_db.close()
    assert resumed == clean
