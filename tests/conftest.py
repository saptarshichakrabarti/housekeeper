"""Shared fixtures for the housekeeper test suite.

Tests never touch a real external drive: everything runs against temporary directories and
the synthetic fixture generator in ``scripts/create_test_fixture.py``.
"""

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "scripts"))

from create_test_fixture import build_fixture

from housekeeper.config import load_config
from housekeeper.database import Database
from housekeeper.scanner import DriveScanner


@pytest.fixture
def config(tmp_path):
    return load_config(workspace_override=tmp_path / "workspace")


@pytest.fixture
def database(config):
    db = Database(config.database_path)
    db.initialize()
    yield db
    db.close()


@pytest.fixture
def fixture_root(tmp_path):
    """A rich synthetic drive tree produced by the shared fixture generator."""
    return build_fixture(tmp_path / "drive", clean=True)


@pytest.fixture
def scanned(config, database, fixture_root):
    """Scan the synthetic fixture and return ``(database, config, root)``."""
    DriveScanner(database, config).scan(fixture_root, incremental=False)
    return database, config, fixture_root


def analyse_and_classify(database, config):
    """Run the full analysis + classification pipeline used by several integration tests."""
    from housekeeper.analysers.exact_duplicates import run_exact_duplicate_analysis
    from housekeeper.analysers.projects import run_project_analysis
    from housekeeper.analysers.registry import run_content_analysis
    from housekeeper.policies import classify_all_entries

    run_exact_duplicate_analysis(database, config)
    run_content_analysis(database, config, None)
    run_project_analysis(database, config)
    return classify_all_entries(database, config)


# --- Phase 0: work-counter harness -------------------------------------------------------------
# Timings are noise in CI; units of work are not. `run_with_counters` runs the real scan + content
# analysis pipeline and returns machine-independent counters, so a test can assert "an unchanged
# rescan reads zero source bytes" rather than a wall-clock threshold.


def run_with_counters(root: Path, workspace: Path):
    """Scan and analyse ``root`` into ``workspace``, returning the recorded work counters.

    A ``Counter`` rather than a dict, so an assertion on work that never happened reads
    ``counts["source_bytes_read"] == 0`` instead of tripping over a missing key.
    """
    from housekeeper.analysers.registry import run_content_analysis
    from housekeeper.core import counters

    config = load_config(workspace_override=workspace)
    database = Database(config.database_path)
    database.initialize()
    with counters.recording() as counts:
        try:
            with counters.stage("scan"):
                DriveScanner(database, config).scan(root)
            with counters.stage("content_analysis"):
                run_content_analysis(database, config, None)
        finally:
            database.close()
    return counts


def build_flat_corpus(root: Path, file_count: int, dir_count: int = 1) -> Path:
    """A deterministic corpus of small unique text files spread over ``dir_count`` directories."""
    root.mkdir(parents=True, exist_ok=True)
    directories = []
    for index in range(dir_count):
        directory = root / f"dir_{index:04d}"
        directory.mkdir(exist_ok=True)
        directories.append(directory)
    for index in range(file_count):
        (directories[index % dir_count] / f"file_{index:06d}.txt").write_text(
            f"corpus entry {index}\n", encoding="utf-8"
        )
    return root


def _suffix(index: int) -> str:
    """A third of the corpus is analysable. A corpus of one extension makes the suffix predicate
    free, so a plan test over it cannot tell an index seek from a scan."""
    return ".txt" if index % 3 == 0 else ".dat"


@pytest.fixture(scope="session")
def metadata_corpus(tmp_path_factory):
    """A ≥100k-row inventory database built directly, without creating 100k real files.

    Query plans differ between a toy database and one with real statistics, so the plan tests need
    a corpus this size; creating the files on disk would make the suite unusable.
    """
    from housekeeper.core import counters as _counters  # noqa: F401  (registers connections)

    path = tmp_path_factory.mktemp("metadata-corpus") / "inventory.sqlite"
    database = Database(path)
    database.initialize()
    conn = database.connect()
    entries = 120_000
    older = database.create_scan_run("/synthetic", "synthetic-corpus", "plan-test")
    run = database.create_scan_run("/synthetic", "synthetic-corpus", "plan-test")
    source = conn.execute(
        "INSERT INTO source_roots(display_name,source_fingerprint,last_mount_path) VALUES('synthetic','synthetic-corpus','/synthetic') RETURNING id"
    ).fetchone()[0]
    for scan_run in (older, run):
        conn.executemany(
            """INSERT INTO filesystem_entries(scan_run_id,parent_entry_id,source_root_id,source_root,absolute_path,
               relative_path,name,suffix,entry_type,size_bytes,modified_at,scan_status)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,'OK')""",
            [
                (
                    scan_run,
                    None,
                    source,
                    "/synthetic",
                    f"/synthetic/{i // 500:05d}/item-{i:07d}{_suffix(i)}",
                    f"{i // 500:05d}/item-{i:07d}{_suffix(i)}",
                    f"item-{i:07d}{_suffix(i)}",
                    _suffix(i),
                    "file",
                    i % 4096,
                    1_700_000_000.0 + i,
                )
                for i in range(entries // 2)
            ],
        )
        conn.executemany(
            """INSERT INTO filesystem_entries(scan_run_id,source_root_id,source_root,absolute_path,
               relative_path,name,entry_type,size_bytes,scan_status)
               VALUES(?,?,?,?,?,?,'directory',0,'OK')""",
            [
                (scan_run, source, "/synthetic", f"/synthetic/{d:05d}", f"{d:05d}", f"{d:05d}")
                for d in range(entries // 1000)
            ],
        )
    conn.execute(
        "UPDATE filesystem_entries SET parent_entry_id=(SELECT d.id FROM filesystem_entries d "
        "WHERE d.scan_run_id=filesystem_entries.scan_run_id AND d.entry_type='directory' "
        "AND d.relative_path=substr(filesystem_entries.relative_path,1,5)) WHERE entry_type='file'"
    )
    conn.execute(
        """INSERT INTO file_signatures(entry_id,full_hash,quick_hash,hash_algorithm,hash_status)
           SELECT id,printf('%064x',size_bytes*31+id%7),printf('%064x',size_bytes),'sha256','OK'
           FROM filesystem_entries WHERE entry_type='file'"""
    )
    conn.execute(
        """INSERT INTO scan_entry_changes(scan_run_id,entry_id,relative_path,change_status)
           SELECT scan_run_id,id,relative_path,'MISSING' FROM filesystem_entries
           WHERE scan_run_id=? AND entry_type='file' AND id%97=0""",
        (older,),
    )
    # Content objects are deduplicated across snapshots, so the *same* logical file in both runs
    # links to one content object. That is the production shape, and it is the shape the content
    # work plan needs: without it a two-snapshot corpus cannot tell "this object is reachable from
    # the current inventory" from "the planner handed back last week's row".
    conn.execute(
        """CREATE TEMP TABLE content_map AS
           SELECT DISTINCT relative_path,size_bytes FROM filesystem_entries WHERE entry_type='file'"""
    )
    conn.execute("CREATE INDEX temp_content_map_path ON content_map(relative_path)")
    conn.execute(
        """INSERT INTO content_objects(id,hash_algorithm,full_hash,size_bytes)
           SELECT rowid,'sha256',printf('%064x',rowid),size_bytes FROM content_map"""
    )
    conn.execute(
        """INSERT INTO entry_content_links(entry_id,content_object_id,link_status)
           SELECT e.id,m.rowid,'VERIFIED' FROM filesystem_entries e
           JOIN content_map m ON m.relative_path=e.relative_path
           WHERE e.entry_type='file'"""
    )
    conn.execute("UPDATE scan_runs SET status='COMPLETE',completed_at=CURRENT_TIMESTAMP")
    # The newer run is this source's current inventory, exactly as the scanner would leave it —
    # so current_entries resolves to half the corpus and the plan tests see a real two-snapshot
    # database rather than one where "current" is empty.
    conn.execute("UPDATE source_roots SET latest_complete_scan_run_id=? WHERE id=?", (run, source))
    database.refresh_current_inventory_views()
    conn.commit()
    conn.execute("ANALYZE")  # plans without statistics are not the plans production gets
    conn.commit()
    yield database, run, source
    database.close()
