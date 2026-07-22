"""End-to-end CLI tests: command dispatch, exit codes, and user-facing error handling."""

import csv

from housekeeper.cli import main


def _ws(tmp_path):
    return ["--workspace", str(tmp_path / "ws")]


def _source(tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.bin").write_bytes(b"duplicated")
    (root / "b.bin").write_bytes(b"duplicated")
    (root / "unique.txt").write_text("only one of me", encoding="utf-8")
    return root


def test_full_read_only_workflow(tmp_path, capsys):
    ws = _ws(tmp_path)
    root = _source(tmp_path)
    assert main([*ws, "init-workspace"]) == 0
    assert main([*ws, "scan", str(root)]) == 0
    assert main([*ws, "analyse", "all"]) == 0
    assert main([*ws, "classify"]) == 0
    assert main([*ws, "report", "all"]) == 0
    assert main([*ws, "stats"]) == 0
    assert main([*ws, "scan-status"]) == 0
    output = capsys.readouterr().out
    assert "entry_type" in output  # stats rendered as a readable table, not a Row repr
    assert "sqlite3.Row" not in output


def test_export_validate_and_move_dry_run(tmp_path):
    ws = _ws(tmp_path)
    root = _source(tmp_path)
    main([*ws, "scan", str(root)])
    main([*ws, "analyse", "exact-duplicates"])
    main([*ws, "classify"])
    manifest = tmp_path / "review.csv"
    assert main([*ws, "export-review", "--output", str(manifest)]) == 0
    # Approve the review-safe rows (simulating a human editing the manifest).
    rows = list(csv.DictReader(manifest.open(encoding="utf-8")))
    for row in rows:
        row["approved"] = "true"
    with manifest.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    assert main([*ws, "validate-manifest", str(manifest)]) == 0
    review_root = tmp_path / "review"
    assert main([*ws, "move-to-review", str(manifest), str(review_root), "--dry-run"]) == 0
    # A dry run never moves anything.
    assert (root / "a.bin").exists() and (root / "b.bin").exists()


def test_expected_error_returns_exit_code_not_traceback(tmp_path, capsys):
    ws = _ws(tmp_path)
    main([*ws, "init-workspace"])
    rc = main([*ws, "validate-manifest", str(tmp_path / "does-not-exist.csv")])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")


def test_non_loopback_dashboard_refused(tmp_path):
    ws = _ws(tmp_path)
    main([*ws, "init-workspace"])
    try:
        main([*ws, "dashboard", "--host", "0.0.0.0"])
    except SystemExit as exc:
        assert "non-loopback" in str(exc.code)
    else:  # pragma: no cover - must not silently bind
        raise AssertionError("dashboard should refuse a non-loopback host")


def test_diff_reports_changes(tmp_path):
    ws = _ws(tmp_path)
    root = _source(tmp_path)
    main([*ws, "scan", str(root)])
    (root / "c.bin").write_bytes(b"new file")
    main([*ws, "scan", str(root)])
    scans = ["--workspace", str(tmp_path / "ws")]
    assert main([*scans, "diff", "1", "2"]) == 0


def test_analyse_all_runs_advanced_analysers(tmp_path):
    from housekeeper.config import load_config
    from housekeeper.database import Database

    ws = _ws(tmp_path)
    root = _source(tmp_path)
    main([*ws, "scan", str(root)])
    assert main([*ws, "analyse", "all"]) == 0
    assert main([*ws, "classify"]) == 0
    db = Database(load_config(workspace_override=tmp_path / "ws").database_path)
    # analyse all + classify populate the advanced tables.
    assert db.fetch_one("SELECT COUNT(*) n FROM collection_clusters")["n"] >= 1
    assert db.fetch_one("SELECT COUNT(*) n FROM record_series_assignments")["n"] >= 1
    assert db.fetch_one("SELECT COUNT(*) n FROM review_priority")["n"] >= 1
    assert db.fetch_one("SELECT COUNT(*) n FROM entry_lifecycle")["n"] >= 1


def test_collections_retention_and_known_cli(tmp_path):
    ws = _ws(tmp_path)
    root = _source(tmp_path)
    main([*ws, "scan", str(root)])
    main([*ws, "analyse", "record-series"])
    assert main([*ws, "collections", "retention"]) == 0
    assert main([*ws, "known", "assert", "KNOWN_INSTALLER", "PATH_PATTERN", "setup"]) == 0
    assert main([*ws, "known", "list"]) == 0
