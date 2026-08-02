"""Traversal guards for long, messy, multi-day scans: cycles, flat directories, error storms.

Each guard exists because the failure it prevents is real on a terabyte drive walked for hours: a
bind-mount loop that never terminates, a single directory too large to sort in memory, and a drive
that drops off the bus and turns every remaining entry into an error.
"""

from __future__ import annotations

import housekeeper.scanner as scanner_module
from housekeeper.core import counters
from housekeeper.jobs import JobPaused
from housekeeper.scanner import DriveScanner


def test_a_directory_cycle_terminates(config, database, tmp_path, monkeypatch):
    """A bind mount presents a real directory whose (device, inode) repeats an ancestor's.

    Simulated by making inspect_entry report a fixed identity for a directory that also appears as
    a child of itself, which is exactly the shape a bind-mount loop has to the walker.
    """
    root = tmp_path / "src"
    (root / "loop").mkdir(parents=True)
    (root / "loop" / "again").mkdir()
    (root / "file.txt").write_text("real content")

    real_inspect = DriveScanner.inspect_entry
    # Every directory named "loop" or "again" reports the identical (device, inode): to the walker
    # that is one directory reachable under itself — a cycle it must not descend forever.
    def fake_inspect(self, path, scan_root, *args, **kwargs):
        record = real_inspect(self, path, scan_root, *args, **kwargs)
        if path.name in {"loop", "again"}:
            object.__setattr__(record, "device_id", 4242)
            object.__setattr__(record, "inode_or_file_id", 99)
        return record

    monkeypatch.setattr(DriveScanner, "inspect_entry", fake_inspect)
    with counters.recording() as counts:
        result = DriveScanner(database, config).scan(root, incremental=False)
    # It terminated (the assertion is that we got here at all), recorded the real file, and skipped
    # at least one directory as an already-seen identity.
    assert result["files"] >= 1
    assert counts["directories_skipped_as_cycles"] >= 1


def test_a_huge_flat_directory_is_streamed_not_materialised(config, database, tmp_path, monkeypatch):
    """Below the sort limit a directory is sorted; a monkeypatched tiny limit forces the stream path."""
    root = tmp_path / "src"
    root.mkdir()
    for index in range(25):
        (root / f"f{index:03d}.txt").write_text(str(index))

    monkeypatch.setattr(scanner_module, "SCAN_DIR_SORT_LIMIT", 10)
    with counters.recording() as counts:
        result = DriveScanner(database, config).scan(root, incremental=False)

    assert result["files"] == 25  # every entry is still recorded, sorted or not
    assert counts["directories_streamed_unsorted"] >= 1
    names = {row["name"] for row in database.fetch_all("SELECT name FROM filesystem_entries WHERE entry_type='file'")}
    assert len(names) == 25


def test_an_error_storm_parks_the_scan(config, database, tmp_path, monkeypatch):
    """A run of consecutive read errors pauses the scan resumably instead of walking on for hours."""
    root = tmp_path / "src"
    root.mkdir()
    for index in range(50):
        (root / f"f{index:03d}.txt").write_text(str(index))

    config.section("scanner")["pause_after_consecutive_errors"] = 5

    real_inspect = DriveScanner.inspect_entry
    calls = {"n": 0}

    def failing_inspect(self, path, scan_root, *args, **kwargs):
        record = real_inspect(self, path, scan_root, *args, **kwargs)
        if record.entry_type == "file":
            calls["n"] += 1
            object.__setattr__(record, "read_error", "simulated I/O error")
        return record

    monkeypatch.setattr(DriveScanner, "inspect_entry", failing_inspect)
    scanner = DriveScanner(database, config)
    try:
        scanner.scan(root, incremental=False)
    except JobPaused:
        pass
    else:  # pragma: no cover - the storm must trip the breaker
        raise AssertionError("the error storm should have paused the scan")

    # It stopped near the threshold, not after all fifty files.
    assert calls["n"] < 50
    run = database.fetch_one("SELECT status FROM scan_runs ORDER BY id DESC LIMIT 1")
    assert run["status"] == "INTERRUPTED"


def test_a_parked_scan_resumes_when_the_fault_clears(config, database, tmp_path, monkeypatch):
    """The breaker's pause is resumable: once the drive is back, a rescan completes the inventory."""
    root = tmp_path / "src"
    root.mkdir()
    for index in range(40):
        (root / f"f{index:03d}.txt").write_text(str(index))

    config.section("scanner")["pause_after_consecutive_errors"] = 5
    real_inspect = DriveScanner.inspect_entry

    def failing_inspect(self, path, scan_root, *args, **kwargs):
        record = real_inspect(self, path, scan_root, *args, **kwargs)
        if record.entry_type == "file":
            object.__setattr__(record, "read_error", "drive offline")
        return record

    monkeypatch.setattr(DriveScanner, "inspect_entry", failing_inspect)
    scanner = DriveScanner(database, config)
    try:
        scanner.scan(root, incremental=False)
    except JobPaused:
        pass

    monkeypatch.undo()  # the drive is back
    result = DriveScanner(database, config).scan(root, resume=True, incremental=False)
    assert result["files"] == 40
    run = database.fetch_one("SELECT status FROM scan_runs ORDER BY id DESC LIMIT 1")
    assert run["status"] == "COMPLETE"


def test_scattered_errors_do_not_trip_the_breaker(config, database, tmp_path, monkeypatch):
    """The breaker counts *consecutive* errors; a readable file between them resets the count."""
    root = tmp_path / "src"
    root.mkdir()
    for index in range(30):
        (root / f"f{index:03d}.txt").write_text(str(index))

    config.section("scanner")["pause_after_consecutive_errors"] = 5
    real_inspect = DriveScanner.inspect_entry

    def alternating(self, path, scan_root, *args, **kwargs):
        record = real_inspect(self, path, scan_root, *args, **kwargs)
        # Every other file errors — never five in a row, so the breaker must never fire.
        if record.entry_type == "file" and int(path.stem[1:]) % 2 == 0:
            object.__setattr__(record, "read_error", "occasional error")
        return record

    monkeypatch.setattr(DriveScanner, "inspect_entry", alternating)
    result = DriveScanner(database, config).scan(root, incremental=False)  # must complete
    assert result["files"] == 30
    run = database.fetch_one("SELECT status FROM scan_runs ORDER BY id DESC LIMIT 1")
    assert run["status"] == "COMPLETE"
