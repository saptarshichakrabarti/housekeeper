"""Bounding retained scan history, without destroying the audit trail.

History is retained by default: a superseded snapshot's verdict is the evidence this tool exists to
produce, so nothing prunes itself. `database prune-snapshots` is the explicit way to bound it, and
the interesting half is what it *refuses* — `review_decisions.target_id` is a bare integer, not a
foreign key, so deleting an entry a human decided about would leave the decision pointing at
nothing.
"""

from __future__ import annotations

from housekeeper.database import Database
from housekeeper.review.decisions import create_session, record_decision
from housekeeper.scanner import DriveScanner


def _scan_times(database, config, root, count: int) -> None:
    scanner = DriveScanner(database, config)
    for _ in range(count):
        scanner.scan(root)


def _corpus(tmp_path, name="drive", files=3):
    root = tmp_path / name
    root.mkdir()
    for index in range(files):
        (root / f"file-{index}.txt").write_text(f"contents {index}\n", encoding="utf-8")
    return root


def test_a_plan_changes_nothing(config, database, tmp_path):
    root = _corpus(tmp_path)
    _scan_times(database, config, root, 6)
    before = database.fetch_one("SELECT COUNT(*) n FROM filesystem_entries")["n"]

    plan = database.snapshot_retention_plan(keep_per_source=2)
    assert plan["prunable"], "6 scans keeping 2 should leave something prunable"
    assert database.fetch_one("SELECT COUNT(*) n FROM filesystem_entries")["n"] == before


def test_pruning_keeps_the_requested_window_and_removes_the_rest(config, database, tmp_path):
    root = _corpus(tmp_path)
    _scan_times(database, config, root, 6)
    assert database.fetch_one("SELECT COUNT(*) n FROM scan_runs")["n"] == 6

    plan = database.prune_snapshots(keep_per_source=2)
    remaining = [int(r["id"]) for r in database.fetch_all("SELECT id FROM scan_runs ORDER BY id")]
    assert len(remaining) == 2, f"expected 2 snapshots, got {remaining}"
    assert remaining == sorted(remaining)[-2:]
    assert len(plan["prunable"]) == 4
    # Entries went with them, and their dependent rows cascaded.
    assert database.fetch_one("SELECT COUNT(*) n FROM filesystem_entries")["n"] == 3 * 2
    orphans = database.fetch_one(
        "SELECT COUNT(*) n FROM classifications c "
        "WHERE NOT EXISTS(SELECT 1 FROM filesystem_entries e WHERE e.id=c.entry_id)"
    )["n"]
    assert orphans == 0


def test_pruning_removes_the_change_rows_of_pruned_runs(config, database, tmp_path):
    """scan_entry_changes is one row per entry per run — the table most at risk of unbounded growth
    on a long-lived workspace. It must be pruned with its run, not left behind."""
    root = _corpus(tmp_path)
    _scan_times(database, config, root, 5)
    before = database.fetch_one("SELECT COUNT(*) n FROM scan_entry_changes")["n"]
    assert before > 0, "the incremental rescans should have recorded change rows"

    database.prune_snapshots(keep_per_source=1)
    kept = {int(r["id"]) for r in database.fetch_all("SELECT id FROM scan_runs")}
    orphaned = database.fetch_one(
        f"SELECT COUNT(*) n FROM scan_entry_changes WHERE scan_run_id NOT IN ({','.join('?' for _ in kept)})",
        tuple(kept),
    )["n"]
    assert orphaned == 0, "a pruned run's change rows survived the prune"


def test_the_current_inventory_is_never_pruned(config, database, tmp_path):
    root = _corpus(tmp_path)
    _scan_times(database, config, root, 4)
    current = database.fetch_one(
        "SELECT latest_complete_scan_run_id i FROM source_roots"
    )["i"]

    database.prune_snapshots(keep_per_source=0)
    assert database.fetch_one("SELECT COUNT(*) n FROM scan_runs WHERE id=?", (current,))["n"] == 1
    assert database.fetch_one("SELECT COUNT(*) n FROM current_entries")["n"] == 3


def test_a_snapshot_with_a_review_decision_is_held(config, database, tmp_path):
    """The case that makes this safe rather than merely bounded."""
    root = _corpus(tmp_path)
    _scan_times(database, config, root, 5)
    oldest_entry = database.fetch_one(
        "SELECT id,scan_run_id FROM filesystem_entries ORDER BY id LIMIT 1"
    )
    session = create_session(database, "retention test")
    record_decision(database, session, "ENTRY", int(oldest_entry["id"]), "MARK_KEEP")

    plan = database.snapshot_retention_plan(keep_per_source=1)
    holds = {item["scan_run_id"]: item["reason"] for item in plan["held"]}
    assert int(oldest_entry["scan_run_id"]) in holds
    assert holds[int(oldest_entry["scan_run_id"])] == "a recorded review decision"

    database.prune_snapshots(keep_per_source=1)
    assert database.fetch_one(
        "SELECT COUNT(*) n FROM filesystem_entries WHERE id=?", (oldest_entry["id"],)
    )["n"] == 1, "an entry a human decided about was deleted"
    assert database.fetch_one("SELECT COUNT(*) n FROM review_decisions")["n"] == 1


def test_pruning_leaves_content_objects_and_their_artifacts(config, database, tmp_path):
    """Content identity is snapshot-independent by design, so it survives a prune.

    An artifact is keyed on content, not on the snapshot that happened to surface it — dropping
    those would make the next run re-parse a corpus for no reason.
    """
    from housekeeper.analysers.registry import run_content_analysis

    root = _corpus(tmp_path)
    _scan_times(database, config, root, 4)
    run_content_analysis(database, config, "documents")
    objects = database.fetch_one("SELECT COUNT(*) n FROM content_objects")["n"]
    artifacts = database.fetch_one("SELECT COUNT(*) n FROM analysis_artifacts")["n"]
    assert objects and artifacts

    database.prune_snapshots(keep_per_source=1)
    assert database.fetch_one("SELECT COUNT(*) n FROM content_objects")["n"] == objects
    assert database.fetch_one("SELECT COUNT(*) n FROM analysis_artifacts")["n"] == artifacts


def test_prune_refreshes_the_current_inventory_views(config, database, tmp_path):
    root = _corpus(tmp_path)
    _scan_times(database, config, root, 4)
    database.prune_snapshots(keep_per_source=1)
    assert database.fetch_one("SELECT COUNT(*) n FROM current_entries")["n"] == 3


def test_cli_prune_is_a_dry_run_without_yes(config, tmp_path, capsys):
    from housekeeper.cli import main

    root = _corpus(tmp_path)
    database = Database(config.database_path)
    database.initialize()
    _scan_times(database, config, root, 4)
    database.close()

    assert main(["--workspace", str(config.workspace), "database", "prune-snapshots"]) == 0
    assert "dry_run" in capsys.readouterr().out

    database = Database(config.database_path)
    database.initialize()
    try:
        assert database.fetch_one("SELECT COUNT(*) n FROM scan_runs")["n"] == 4
    finally:
        database.close()
