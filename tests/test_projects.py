"""Project detection: kinds, reproducibility signals, storage breakdown, .git protection."""

from housekeeper.analyzers.projects import (
    calculate_project_storage_breakdown,
    classify_project_kind,
    detect_reproducibility_signals,
    run_project_analysis,
)
from housekeeper.scanner import DriveScanner


def test_classify_project_kind():
    assert classify_project_kind(["pyproject.toml"]) == "python"
    assert classify_project_kind(["package.json"]) == "javascript"
    assert classify_project_kind(["Cargo.toml"]) == "rust"
    assert classify_project_kind(["go.mod"]) == "go"
    assert classify_project_kind(["Makefile"]) == "source"


def test_reproducibility_signals():
    signals = detect_reproducibility_signals(["pyproject.toml", "uv.lock", ".git"])
    assert signals["has_dependency_spec"]
    assert signals["has_lockfile"]
    assert signals["has_git"]


def test_python_project_detected_with_storage_breakdown(config, database, tmp_path):
    root = tmp_path / "src"
    proj = root / "app"
    (proj / ".git").mkdir(parents=True)
    (proj / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (proj / "uv.lock").write_text("lock", encoding="utf-8")
    (proj / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (proj / "__pycache__").mkdir()
    (proj / "__pycache__" / "main.pyc").write_bytes(b"generated")
    (proj / ".venv" / "lib").mkdir(parents=True)
    (proj / ".venv" / "lib" / "x.py").write_text("env", encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    run_project_analysis(database, config)
    project = database.fetch_one("SELECT * FROM projects WHERE name='app'")
    assert project["kind"] == "python"
    assert project["git_status"] == "present"
    assert project["generated_size_bytes"] > 0
    assert project["environment_size_bytes"] > 0
    assert project["source_size_bytes"] > 0


def test_storage_breakdown_buckets(config, database, tmp_path):
    root = tmp_path / "src"
    proj = root / "p"
    (proj / "node_modules" / "dep").mkdir(parents=True)
    (proj / "node_modules" / "dep" / "index.js").write_bytes(b"envenv")
    (proj / "dist").mkdir()
    (proj / "dist" / "bundle.js").write_bytes(b"gen")
    (proj / "app.js").write_bytes(b"source")
    DriveScanner(database, config).scan(root, incremental=False)
    scan_id = database.fetch_one("SELECT MAX(id) AS m FROM scan_runs")["m"]
    breakdown = calculate_project_storage_breakdown(database, scan_id, "p")
    assert breakdown["environment"] == len(b"envenv")
    assert breakdown["generated"] == len(b"gen")
    assert breakdown["source"] == len(b"source")
