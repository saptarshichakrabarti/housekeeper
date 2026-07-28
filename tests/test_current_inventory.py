"""Standalone analysis must scope to the current inventory, not accumulated scan history.

Re-scanning the same drive keeps a snapshot per scan. An unscoped exact-duplicate pass would then
group a unique, single-copy file with its own prior-scan snapshot and classify the current copy as a
removable duplicate. These tests pin that a unique file survives re-scans as ``KEEP`` through both
the GUI runner and the CLI, while genuine same-snapshot duplicates are still detected.
"""

import time

from housekeeper.analysers.exact_duplicates import run_exact_duplicate_analysis
from housekeeper.analysers.scope import AnalyserScope
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


def test_scanner_records_the_current_inventory_when_a_run_completes(config, database, tmp_path):
    """3.1: which scan is current is a stored fact, written with the COMPLETE transition."""
    root = tmp_path / "drive"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    scanner = DriveScanner(database, config)
    scanner.scan(root, incremental=False)
    first = scanner.last_run_id
    assert database.fetch_one("SELECT latest_complete_scan_run_id n FROM source_roots")["n"] == first

    scanner.scan(root)
    assert (
        database.fetch_one("SELECT latest_complete_scan_run_id n FROM source_roots")["n"]
        == scanner.last_run_id
        != first
    )


def test_interrupted_run_does_not_become_the_current_inventory(config, database, tmp_path):
    """An incomplete scan is not an inventory; nothing current-state may read from it."""
    from housekeeper.analysers.scope import current_inventory_runs

    root = tmp_path / "drive"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    scanner = DriveScanner(database, config)
    scanner.scan(root, incremental=False)
    complete = scanner.last_run_id
    database.connect().execute(
        "INSERT INTO scan_runs(source_root,source_root_fingerprint,status) "
        "SELECT source_root,source_root_fingerprint,'INTERRUPTED' FROM scan_runs WHERE id=?",
        (complete,),
    )
    database.connect().commit()
    assert current_inventory_runs(database) == frozenset({complete})


def test_analyser_called_without_a_scope_sees_only_the_current_inventory(config, database, tmp_path):
    """3.2: an analyser that can be called without a scope will be, so the default is the safe one."""
    from housekeeper.analysers.scope import resolve_scope

    root = tmp_path / "drive"
    root.mkdir()
    (root / "only-copy.bin").write_bytes(b"unique payload")
    scanner = DriveScanner(database, config)
    scanner.scan(root, incremental=False)
    scanner.scan(root)

    entry_sql, params = resolve_scope(database, None).entry_id_sql()
    visible = database.fetch_all(f"SELECT id FROM filesystem_entries WHERE id IN ({entry_sql})", params)
    assert len(visible) == 1, "the default scope reached back into scan history"

    # ...and the unscoped view really does contain both snapshots, so the test above means something.
    from housekeeper.analysers.scope import AnalyserScope

    all_sql, all_params = AnalyserScope.all_history().entry_id_sql()
    assert (
        len(database.fetch_all(f"SELECT id FROM filesystem_entries WHERE id IN ({all_sql})", all_params))
        == 2
    )


def test_scoped_analysis_survives_a_corpus_past_the_sql_parameter_ceiling(metadata_corpus):
    """2.6: the scope was bound as `IN (?,?,…)` over every entry id — 1.2M parameters, and a crash."""
    from housekeeper.analysers.images import run_image_analysis
    from housekeeper.analysers.scope import AnalyserScope
    from housekeeper.config import load_config

    database, run_id, _source_id = metadata_corpus
    scope = AnalyserScope(scan_run_ids=frozenset({run_id}))
    # 60,000 in-scope file entries — far past the 32,766-parameter limit on many SQLite builds.
    run_image_analysis(database, load_config(), scope)


def test_derived_collections_never_span_snapshots(config, tmp_path):
    """Definition of done #10 for the tables that group entries together.

    A duplicate group or directory summary that reaches across snapshots is the G2 bug itself: it
    relates a file to its own earlier self. (Per-entry verdicts like ``classifications`` are a
    different matter — a superseded snapshot's verdict is *history*, and deleting it would destroy
    the audit trail the tool exists to produce. See the cost test below for what must not happen to
    those rows instead.)
    """
    from housekeeper.quickstart import run_quickstart

    root = tmp_path / "drive"
    root.mkdir()
    for index in range(3):
        (root / f"file-{index}.txt").write_text(f"contents {index}", encoding="utf-8")
    (root / "copy.txt").write_text("contents 0", encoding="utf-8")  # a genuine same-scan duplicate

    db = Database(config.database_path)
    db.initialize()
    try:
        run_quickstart(db, config, root, generate_reports=False)
        summary = run_quickstart(db, config, root, generate_reports=False)
        current = summary["scan_run_id"]
        assert db.fetch_one("SELECT COUNT(*) n FROM scan_runs")["n"] == 2, "expected two snapshots"
        for table in ("exact_duplicate_members", "directory_summaries"):
            spanning = db.fetch_one(
                f"""SELECT COUNT(*) n FROM {table} t
                    JOIN filesystem_entries e ON e.id=t.entry_id WHERE e.scan_run_id<>?""",
                (current,),
            )
            assert int(spanning["n"]) == 0, f"{table} reaches into a historical snapshot"
        # The genuine duplicate is still found — scoping must not have made the analysis blind.
        assert int(db.fetch_one("SELECT COUNT(*) n FROM exact_duplicate_groups")["n"]) == 1
    finally:
        db.close()


def test_rescanning_does_not_reclassify_history(config, tmp_path):
    """Phase 3's cost claim: work tracks the size of the drive, not how often you have scanned it."""
    from housekeeper.quickstart import run_quickstart

    root = tmp_path / "drive"
    root.mkdir()
    for index in range(4):
        (root / f"file-{index}.txt").write_text(f"contents {index}", encoding="utf-8")

    db = Database(config.database_path)
    db.initialize()
    try:
        first = run_quickstart(db, config, root, generate_reports=False)["scan_run_id"]
        historical = {
            int(row["entry_id"]): row["classified_at"]
            for row in db.fetch_all(
                """SELECT c.entry_id,c.classified_at FROM classifications c
                   JOIN filesystem_entries e ON e.id=c.entry_id WHERE e.scan_run_id=?""",
                (first,),
            )
        }
        assert historical, "the first run should have classified something"

        run_quickstart(db, config, root, generate_reports=False)

        after = {
            int(row["entry_id"]): row["classified_at"]
            for row in db.fetch_all(
                """SELECT c.entry_id,c.classified_at FROM classifications c
                   JOIN filesystem_entries e ON e.id=c.entry_id WHERE e.scan_run_id=?""",
                (first,),
            )
        }
        assert after == historical, "the second run re-derived verdicts for the first run's rows"
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

    run_exact_duplicate_analysis(database, config, scope=AnalyserScope.current(database))
    groups = database.fetch_all("SELECT * FROM exact_duplicate_groups")
    assert len(groups) == 1
    assert groups[0]["member_count"] == 2

    classify_all_entries(database, config)
    classifications = _current_classifications(database)
    assert "REVIEW_SAFE" in classifications, classifications


def test_content_object_scope_survives_more_ids_than_sql_allows(metadata_corpus):
    """The one scope facet whose width the caller chooses, so the caller can exceed the limit.

    ``--content-object-id`` takes a list and it was expanded to one placeholder per id. A scripted
    caller passing 40,000 ids got ``OperationalError: too many SQL variables`` rather than a result;
    on an older SQLite build the ceiling is 999. json_each binds the whole list as one parameter.
    """
    from housekeeper.analysers.scope import AnalyserScope

    database, run_id, _source_id = metadata_corpus
    ids = frozenset(
        int(row["id"]) for row in database.fetch_all("SELECT id FROM content_objects LIMIT 40000")
    )
    assert len(ids) > 32_766, f"only {len(ids)} content objects — cannot exercise the ceiling"

    scope = AnalyserScope(scan_run_ids=frozenset({run_id}), content_object_ids=ids)
    sql, params = scope.content_object_id_sql()
    assert len(params) < 100, f"{len(params)} bound parameters — the list is still being expanded"
    rows = database.fetch_all(f"SELECT COUNT(*) n FROM ({sql})", params)
    assert rows[0]["n"] > 0
