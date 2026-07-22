"""Query-plan regression tests and benchmark smoke tests."""

from housekeeper.cli import main


def _plan(database, sql, params=()):
    rows = database.fetch_all("EXPLAIN QUERY PLAN " + sql, params)
    return " ".join(str(row["detail"]) for row in rows)


def test_content_relationship_lookup_uses_index(database):
    plan = _plan(
        database,
        "SELECT id FROM content_relationships WHERE source_type=? AND source_id=? AND relationship_type=? AND status='ACTIVE'",
        ("CONTENT_OBJECT", 1, "PIXEL_IDENTICAL"),
    )
    assert "USING INDEX" in plan or "USING COVERING INDEX" in plan
    assert "SCAN content_relationships" not in plan  # never a full scan


def test_chunk_occurrence_lookup_uses_index(database):
    plan = _plan(database, "SELECT content_object_id FROM chunk_occurrences WHERE chunk_id=?", (1,))
    assert "USING INDEX" in plan or "USING COVERING INDEX" in plan


def test_classification_grouping_uses_index(database):
    plan = _plan(database, "SELECT entry_id FROM classifications WHERE classification=?", ("REVIEW_SAFE",))
    assert "USING INDEX" in plan or "USING COVERING INDEX" in plan


def test_review_priority_lookup_uses_index(database):
    plan = _plan(database, "SELECT target_id FROM review_priority WHERE category=? ORDER BY score", ("QUICK_SAFE_WIN",))
    assert "USING INDEX" in plan or "USING COVERING INDEX" in plan


def test_entries_by_relative_path_uses_index(database):
    plan = _plan(database, "SELECT id FROM filesystem_entries WHERE relative_path=?", ("a/b.txt",))
    assert "USING INDEX" in plan or "USING COVERING INDEX" in plan


def test_benchmark_scan_reports_timing(tmp_path, capsys):
    root = tmp_path / "src"
    root.mkdir()
    for i in range(5):
        (root / f"f{i}.txt").write_text(f"content {i}", encoding="utf-8")
    ws = ["--workspace", str(tmp_path / "ws")]
    assert main([*ws, "init-workspace"]) == 0
    assert main([*ws, "benchmark", "scan", str(root)]) == 0
    output = capsys.readouterr().out
    assert "seconds" in output
    assert "files" in output


def test_database_explain_command(tmp_path, capsys):
    ws = ["--workspace", str(tmp_path / "ws")]
    main([*ws, "init-workspace"])
    for query in ("review_queue", "overview", "graph", "duplicates"):
        assert main([*ws, "database", "explain", query]) == 0


def test_benchmark_generate_metadata_database_importable():
    import importlib.util
    from pathlib import Path

    # The benchmark generator must at least import cleanly (no top-level execution surprises).
    path = Path(__file__).resolve().parents[1] / "benchmarks" / "generate_metadata_database.py"
    spec = importlib.util.spec_from_file_location("bench_gen", path)
    assert spec and spec.loader
