"""Project detection: kinds, reproducibility signals, storage breakdown, .git protection."""

from housekeeper.analysers.projects import (
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


def test_project_detection_does_not_query_once_per_directory(config, database, tmp_path):
    """Marker detection is one query for the stage, not one per directory.

    It used to ask every directory in the inventory for its children's names purely to intersect
    them with a twenty-name set — 59,399 round trips on the real inventory to find a few hundred
    projects. The intersection is a join. This asserts the statement count barely moves when the
    directory count grows tenfold, which is the property a plan assertion cannot pin.
    """
    from housekeeper.analysers.projects import run_project_analysis
    from housekeeper.core import counters
    from housekeeper.scanner import DriveScanner

    def statements_for(directory_count: int) -> int:
        root = tmp_path / f"tree{directory_count}"
        for index in range(directory_count):
            (root / f"dir{index:04d}").mkdir(parents=True)
            (root / f"dir{index:04d}" / "notes.txt").write_text("x", encoding="utf-8")
        (root / "realproject").mkdir()
        (root / "realproject" / "pyproject.toml").write_text("[project]\n", encoding="utf-8")
        DriveScanner(database, config).scan(root, incremental=False)
        with counters.recording() as counted:
            run_project_analysis(database, config)
        return int(counted["sql_statements"])

    few = statements_for(20)
    many = statements_for(200)
    assert many < few * 3, (
        f"statement count tracks directory count: {few} for 21 directories, {many} for 201"
    )
