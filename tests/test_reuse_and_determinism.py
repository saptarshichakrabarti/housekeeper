"""Settled work stays settled, and identical bytes give identical answers.

These cover the Phase 1/2 fixes that are not about a single corpus shape: which configuration can
invalidate a cached artifact, what "hidden" means, how often a hot loop may ask the database
whether it has been cancelled, and whether a rerun reproduces its own canonical choices.
"""

from __future__ import annotations

from pathlib import Path

from housekeeper.analysers.exact_duplicates import run_exact_duplicate_analysis
from housekeeper.analysers.registry import REGISTRY, run_content_analysis, spec_config_fingerprint
from housekeeper.core import counters
from housekeeper.database import Database
from housekeeper.jobs import check_cancelled, create_job, update_job
from housekeeper.path_utils import descendant_path_range, is_hidden_path
from housekeeper.scanner import DriveScanner


def _documents_spec():
    return next(spec for spec in REGISTRY if spec.name == "documents")


def test_unrelated_configuration_does_not_invalidate_artifacts(config):
    """2.4: the artifact cache was keyed on the whole config, dashboard.port included."""
    spec = _documents_spec()
    before = spec_config_fingerprint(config, spec)
    config.section("dashboard")["port"] = 9999
    config.section("images")["contact_sheet_columns"] = 8
    assert spec_config_fingerprint(config, spec) == before


def test_relevant_configuration_does_invalidate_artifacts(config):
    """The other half: a cache that never invalidates is not a cache either."""
    spec = _documents_spec()
    before = spec_config_fingerprint(config, spec)
    config.section("documents")["max_text_characters"] = 123
    assert spec_config_fingerprint(config, spec) != before


def test_artifacts_are_reused_after_an_unrelated_config_change(config, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "note.txt").write_text("some text", encoding="utf-8")
    database = Database(config.database_path)
    database.initialize()
    try:
        DriveScanner(database, config).scan(root, incremental=False)
        run_content_analysis(database, config, "documents")
        config.section("dashboard")["port"] = 9999
        with counters.recording() as counts:
            run_content_analysis(database, config, "documents")
    finally:
        database.close()
    assert counts["parser_processes_started"] == 0
    assert counts["artifact_cache_misses"] == 0


def test_hidden_is_relative_to_the_source_root(config, tmp_path):
    """2.5: a source root under a dotted ancestor marked the entire drive hidden."""
    root = tmp_path / ".Backup" / "drive"
    root.mkdir(parents=True)
    (root / "visible.txt").write_text("plain", encoding="utf-8")
    hidden_dir = root / ".config"
    hidden_dir.mkdir()
    (hidden_dir / "settings.ini").write_text("x", encoding="utf-8")

    database = Database(config.database_path)
    database.initialize()
    try:
        DriveScanner(database, config).scan(root, incremental=False)
        flags = {
            str(row["relative_path"]): int(row["is_hidden"])
            for row in database.fetch_all("SELECT relative_path,is_hidden FROM filesystem_entries")
        }
    finally:
        database.close()
    assert flags["visible.txt"] == 0
    assert flags[".config"] == 1
    assert flags[".config/settings.ini"] == 1


def test_is_hidden_path_is_documented_as_relative():
    assert is_hidden_path(Path(".config/settings.ini")) is True
    assert is_hidden_path(Path("photos/holiday.jpg")) is False


def test_descendant_range_bounds_are_exact():
    low, high = descendant_path_range("photos/2019")
    assert low <= "photos/2019/a.jpg" < high
    assert not (low <= "photos/2019.jpg" < high)  # sibling, not descendant
    assert not (low <= "photos/2020/a.jpg" < high)
    # The source root itself: everything is a descendant.
    root_low, root_high = descendant_path_range("")
    assert root_low <= "anything/at/all" < root_high


def test_cancellation_check_is_rate_limited(database):
    """1.7: this ran up to four statements per entry, asking a once-per-run question."""
    job_id = create_job(database, "TEST")
    update_job(database, job_id, "RUNNING")
    check_cancelled(database, job_id)  # prime the gate outside the measurement
    with counters.recording() as counts:
        for _ in range(1_000):
            check_cancelled(database, job_id)
    assert counts["sql_statements"] == 0


def _canonical_choices(workspace: Path, root: Path) -> list[tuple[str, str]]:
    from housekeeper.config import load_config

    config = load_config(workspace_override=workspace)
    database = Database(config.database_path)
    database.initialize()
    try:
        DriveScanner(database, config).scan(root, incremental=False)
        run_exact_duplicate_analysis(database, config)
        return [
            (str(row["full_hash"]), str(row["relative_path"]))
            for row in database.fetch_all(
                """SELECT g.full_hash,e.relative_path FROM exact_duplicate_groups g
                   JOIN filesystem_entries e ON e.id=g.canonical_entry_id
                   ORDER BY g.full_hash"""
            )
        ]
    finally:
        database.close()


def test_identical_bytes_produce_identical_canonical_selections(tmp_path):
    """G5: content-object ids are allocated in thread-completion order; choices must not be."""
    root = tmp_path / "drive"
    root.mkdir()
    for group in range(12):
        payload = f"duplicate group {group}".encode() * 64
        for copy in range(3):
            (root / f"g{group:02d}_c{copy}.bin").write_bytes(payload)

    first = _canonical_choices(tmp_path / "ws-a", root)
    second = _canonical_choices(tmp_path / "ws-b", root)
    assert first == second
    assert len(first) == 12


def test_superseded_indexes_are_gone_and_composites_exist(database):
    """1.10: 365 MB of index that duplicated a UNIQUE constraint, byte for byte."""
    names = {
        str(row["name"])
        for row in database.fetch_all("SELECT name FROM sqlite_master WHERE type='index'")
    }
    assert {
        "idx_entries_run_relative",
        "idx_entries_run",
        "idx_content_hash",
        # 172 MB, and it survived the previous review because the *unscoped* dashboard search was
        # the one query that planned through it. Scoping that search to current_entries handed the
        # job to UNIQUE(scan_run_id,relative_path) — see tests/test_dashboard_indexes.py.
        "idx_entries_path",
    } & names == set()
    assert {
        "idx_entries_run_parent_name",
        "idx_entries_run_size",
        "idx_entries_source_path",
        "idx_changes_entry",
    } <= names
    # Kept deliberately: a real dashboard query filters on this column alone.
    assert "idx_entries_suffix" in names
