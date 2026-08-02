"""Opt-in parallel traversal: same inventory as the serial walk, just with the I/O overlapped.

Directory reads run on a small pool; every mutation stays on one thread. The tests hold the two
walks to the same result set (entry ids may differ, the inventory may not), the same per-entry stat
count, and clean cancellation — the hazards a concurrent walk introduces.
"""

from __future__ import annotations

from housekeeper.core import counters
from housekeeper.database import Database
from housekeeper.scanner import DriveScanner


def _tree(root, dirs: int, files_per_dir: int, depth: int = 1):
    root.mkdir(parents=True, exist_ok=True)
    for d in range(dirs):
        sub = root / f"dir_{d:03d}"
        sub.mkdir()
        for f in range(files_per_dir):
            (sub / f"f_{f:03d}.txt").write_text(f"{d}-{f}")
        if depth > 1:
            _tree(sub / "nested", dirs=2, files_per_dir=3, depth=depth - 1)
    return root


def _inventory(db):
    return {
        (row["relative_path"], row["entry_type"], row["size_bytes"])
        for row in db.fetch_all("SELECT relative_path,entry_type,size_bytes FROM filesystem_entries")
    }


def _scan_with_workers(root, workspace, workers):
    from housekeeper.config import load_config

    config = load_config(workspace_override=workspace)
    # Force the profile so traversal_workers is deterministic regardless of measured throughput.
    config.section("performance")["storage_profile"] = "ssd"
    config.section("performance")["overrides"] = {"traversal_workers": workers}
    db = Database(config.database_path)
    db.initialize()
    try:
        DriveScanner(db, config).scan(root, incremental=False)
        return _inventory(db)
    finally:
        db.close()


def test_parallel_and_serial_produce_the_same_inventory(tmp_path):
    root = _tree(tmp_path / "src", dirs=10, files_per_dir=8, depth=2)
    serial = _scan_with_workers(root, tmp_path / "ws1", workers=1)
    parallel = _scan_with_workers(root, tmp_path / "ws4", workers=4)
    assert serial == parallel
    assert len(serial) > 0


def test_parallel_walk_stats_each_entry_once(tmp_path):
    """The 4-stats-per-entry regression must not sneak back in via the parallel path."""
    from housekeeper.config import load_config

    root = _tree(tmp_path / "src", dirs=6, files_per_dir=5)
    config = load_config(workspace_override=tmp_path / "ws")
    config.section("performance")["storage_profile"] = "ssd"
    config.section("performance")["overrides"] = {"traversal_workers": 4}
    db = Database(config.database_path)
    db.initialize()
    try:
        with counters.recording() as counts:
            result = DriveScanner(db, config).scan(root, incremental=False)
    finally:
        db.close()
    non_dir_entries = result["files"] + result["symlinks"]
    # One stat per non-directory entry plus one per directory; never four.
    assert counts["stat_calls"] == result["files"] + result["symlinks"] + result["dirs"]
    assert non_dir_entries > 0


def test_parallel_scan_cancels_without_deadlock(tmp_path, monkeypatch):
    from housekeeper.config import load_config
    from housekeeper.jobs import JobCancelled

    root = _tree(tmp_path / "src", dirs=20, files_per_dir=10)
    config = load_config(workspace_override=tmp_path / "ws")
    config.section("performance")["storage_profile"] = "ssd"
    config.section("performance")["overrides"] = {"traversal_workers": 4}
    config.section("scanner")["batch_size"] = 10
    db = Database(config.database_path)
    db.initialize()

    # Cancel partway through by raising from check_cancelled after a few polls.
    import housekeeper.scanner as scanner_module

    real_check = scanner_module.check_cancelled
    polls = {"n": 0}

    def cancel_after_a_bit(database, job_id):
        polls["n"] += 1
        if polls["n"] > 3:
            raise JobCancelled("cancelled")
        return real_check(database, job_id)

    monkeypatch.setattr(scanner_module, "check_cancelled", cancel_after_a_bit)
    try:
        try:
            DriveScanner(db, config).scan(root, incremental=False)
        except JobCancelled:
            pass
        run = db.fetch_one("SELECT status FROM scan_runs ORDER BY id DESC LIMIT 1")
        assert run["status"] == "INTERRUPTED"  # settled cleanly, no hang
    finally:
        db.close()


def test_parallel_resume_of_a_directory_spanning_many_flushes(tmp_path, monkeypatch):
    """Regression: a directory larger than one batch must survive an interruption mid-directory.

    The parallel frontier is dynamic (pending + inflight + unflushed + staging), and a flush clears
    `unflushed`. Before the fix there was no `staging`, so a directory whose listing spanned two or
    more flushes was dropped from the persisted frontier after its first flush; an interruption
    partway through it lost its un-flushed tail — yet the resumed run was marked COMPLETE. The bug
    only bites when *other* directories keep the frontier non-empty (an empty frontier stores NULL
    and forces a safe full re-walk), so this makes the siblings slow to return: the big directory is
    staged, and interrupted mid-flush, while the siblings are still in flight and on the queue.
    """
    import time

    from housekeeper.config import load_config
    from housekeeper.jobs import JobPaused

    root = tmp_path / "src"
    (root / "big").mkdir(parents=True)
    for i in range(60):
        (root / "big" / f"f{i:03d}.txt").write_text(f"payload {i}\n")
    for s in range(10):
        (root / f"sib{s}").mkdir()
        (root / f"sib{s}" / "only.txt").write_text(f"sibling {s}\n")

    config = load_config(workspace_override=tmp_path / "ws")
    config.section("performance")["storage_profile"] = "ssd"
    config.section("performance")["overrides"] = {"traversal_workers": 4}
    config.section("scanner")["batch_size"] = 8
    db = Database(config.database_path)
    db.initialize()

    # Siblings return slowly, so `big` (instant) is staged first — its flushes fire while the
    # siblings are still in flight and queued, keeping the frontier non-empty but bug-triggering.
    real_scan = DriveScanner._scan_directory_for_worker

    def slow_siblings(self, dir_tuple, r, en, ep):
        if dir_tuple[1].startswith("sib"):
            time.sleep(0.3)
        return real_scan(self, dir_tuple, r, en, ep)

    monkeypatch.setattr(DriveScanner, "_scan_directory_for_worker", slow_siblings)

    real_flush = DriveScanner._flush
    flushes = {"n": 0}

    def interrupting_flush(self, batch, run_id, counts, processed, job_id, current, frontier=None):
        real_flush(self, batch, run_id, counts, processed, job_id, current, frontier)
        flushes["n"] += 1
        if flushes["n"] == 3:  # root's flush, then two of big's — well inside the big directory
            raise JobPaused("interrupt mid-directory")

    monkeypatch.setattr(DriveScanner, "_flush", interrupting_flush)
    try:
        DriveScanner(db, config).scan(root, incremental=False)
    except JobPaused:
        pass
    partial = db.fetch_one("SELECT COUNT(*) n FROM filesystem_entries WHERE name LIKE 'f%'")["n"]
    assert partial < 60, "the interruption should have landed before the big directory finished"

    monkeypatch.setattr(DriveScanner, "_scan_directory_for_worker", real_scan)
    monkeypatch.setattr(DriveScanner, "_flush", real_flush)
    DriveScanner(db, config).scan(root, resume=True, incremental=False)
    assert db.fetch_one("SELECT COUNT(*) n FROM filesystem_entries WHERE name LIKE 'f%'")["n"] == 60
    assert db.fetch_one("SELECT COUNT(*) n FROM filesystem_entries WHERE name='only.txt'")["n"] == 10
    assert db.fetch_one("SELECT status FROM scan_runs ORDER BY id DESC LIMIT 1")["status"] == "COMPLETE"
    db.close()


def test_parallel_resume_matches_a_clean_scan(tmp_path, monkeypatch):
    from housekeeper.config import load_config
    from housekeeper.jobs import JobPaused

    root = _tree(tmp_path / "src", dirs=10, files_per_dir=6)
    config = load_config(workspace_override=tmp_path / "ws")
    config.section("performance")["storage_profile"] = "ssd"
    config.section("performance")["overrides"] = {"traversal_workers": 4}
    config.section("scanner")["batch_size"] = 12
    db = Database(config.database_path)
    db.initialize()

    real = DriveScanner._read_directory
    seen = {"n": 0}

    def interrupt(directory, sort_limit):
        seen["n"] += 1
        if seen["n"] > 4:
            raise JobPaused("stop")
        return real(directory, sort_limit)

    monkeypatch.setattr(DriveScanner, "_read_directory", staticmethod(interrupt))
    try:
        DriveScanner(db, config).scan(root, incremental=False)
    except JobPaused:
        pass
    monkeypatch.setattr(DriveScanner, "_read_directory", staticmethod(real))
    DriveScanner(db, config).scan(root, resume=True, incremental=False)
    resumed = _inventory(db)
    db.close()

    clean = _scan_with_workers(root, tmp_path / "ws_clean", workers=4)
    assert resumed == clean
