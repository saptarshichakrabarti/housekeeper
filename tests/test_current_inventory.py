"""Standalone analysis must scope to the current inventory, not accumulated scan history.

Re-scanning the same drive keeps a snapshot per scan. An unscoped exact-duplicate pass would then
group a unique, single-copy file with its own prior-scan snapshot and classify the current copy as a
removable duplicate. These tests pin that a unique file survives re-scans as ``KEEP`` through both
the GUI runner and the CLI, while genuine same-snapshot duplicates are still detected.
"""

import time

from housekeeper.analysers.exact_duplicates import run_exact_duplicate_analysis
from housekeeper.analysers.scope import analyserScope
from housekeeper.cli import main
from housekeeper.dashboard.runner import OperationRunner
from housekeeper.database import Database
from housekeeper.scanner import DriveScanner


def _wait_idle(runner, timeout=30):
    deadline = time.monotonic() + timeout
    while runner.status()["state"] == "running":
        if time.monotonic() > deadline:
            raise TimeoutError("operation did not finish in time")
        time.sleep(0.02)
    assert runner.status()["state"] != "error", runner.status().get("error")


def _current_classifications(database):
    """Classifications of entries in the latest COMPLETE scan run (the current inventory)."""
    return [
        row["classification"]
        for row in database.fetch_all(
            """SELECT c.classification FROM classifications c
               JOIN filesystem_entries e ON e.id=c.entry_id
               WHERE e.scan_run_id=(SELECT MAX(id) FROM scan_runs WHERE status='COMPLETE')"""
        )
    ]


def test_runner_rescans_leave_unique_files_keep(config, tmp_path):
    """Two GUI scans of a unique-only drive + standalone analyse/classify: nothing goes REVIEW_*."""
    root = tmp_path / "drive"
    root.mkdir()
    for i in range(4):
        (root / f"unique-{i}.bin").write_bytes(f"only-copy-{i}".encode())

    runner = OperationRunner(config)
    for _ in range(2):
        assert runner.submit("quickstart", source=str(root)) == "accepted"
        _wait_idle(runner)
    assert runner.submit("analyse", kind="exact-duplicates") == "accepted"
    _wait_idle(runner)
    assert runner.submit("classify") == "accepted"
    _wait_idle(runner)

    db = Database(config.database_path)
    db.initialize()
    try:
        current = _current_classifications(db)
        assert current, "expected the current inventory to be classified"
        assert all(c in {"KEEP", "ERROR"} for c in current), current
    finally:
        db.close()


def test_cli_rescans_leave_unique_files_keep(config, tmp_path):
    """Same invariant via the CLI: scan twice, then unscoped analyse/classify keeps uniques KEEP."""
    ws = ["--workspace", str(config.workspace)]
    root = tmp_path / "drive"
    root.mkdir()
    for i in range(4):
        (root / f"unique-{i}.bin").write_bytes(f"only-copy-{i}".encode())

    assert main([*ws, "scan", str(root)]) == 0
    assert main([*ws, "scan", str(root)]) == 0
    assert main([*ws, "analyse", "exact-duplicates"]) == 0
    assert main([*ws, "classify"]) == 0

    db = Database(config.database_path)
    db.initialize()
    try:
        current = _current_classifications(db)
        assert current, "expected the current inventory to be classified"
        assert all(c in {"KEEP", "ERROR"} for c in current), current
    finally:
        db.close()


def test_genuine_duplicates_still_detected_under_current_inventory(config, database, tmp_path):
    """A single scan with two byte-identical files still yields one group with a REVIEW_SAFE copy."""
    from housekeeper.policies import classify_all_entries

    root = tmp_path / "drive"
    root.mkdir()
    (root / "a.bin").write_bytes(b"identical-payload")
    (root / "b.bin").write_bytes(b"identical-payload")
    (root / "unique.bin").write_bytes(b"different")
    DriveScanner(database, config).scan(root, incremental=False)

    run_exact_duplicate_analysis(database, config, scope=analyserScope(current_inventory=True))
    groups = database.fetch_all("SELECT * FROM exact_duplicate_groups")
    assert len(groups) == 1
    assert groups[0]["member_count"] == 2

    classify_all_entries(database, config)
    classifications = _current_classifications(database)
    assert "REVIEW_SAFE" in classifications, classifications
