"""`database purge` clears every recorded run and the reports generated from it.

The one thing it must not clear is migration bookkeeping: the schema is untouched by a purge, so a
database that reported itself migrated still is, and forgetting that would re-run migrations.
"""

from __future__ import annotations

from housekeeper.database_maintenance import purge_runs
from tests.conftest import analyse_and_classify


def test_purge_empties_runs_and_reports_but_keeps_migration_state(scanned):
    database, config, _root = scanned
    analyse_and_classify(database, config)
    reports = config.workspace / config.data["workspace"]["reports_dir"]
    (reports / "nested").mkdir(parents=True)
    (reports / "nested" / "summary.html").write_text("stale", encoding="utf-8")
    c = database.connect()
    migrations = c.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0]
    entries = c.execute("SELECT COUNT(*) FROM filesystem_entries").fetchone()[0]
    assert entries > 0

    result = purge_runs(database, config)

    assert result["report_paths_removed"] == 2
    # Reported counts must survive the truncate path the purge deliberately takes.
    assert result["rows_deleted"]["filesystem_entries"] == entries
    assert c.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert not any(reports.iterdir())
    for table in ("scan_runs", "filesystem_entries", "classifications", "content_objects"):
        assert c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0, table
    # Job history goes with the runs, except one marker for the purge itself: without it the next
    # scan's "what changed" digest would present a purged workspace as a drive full of new files.
    assert [
        (row[0], row[1]) for row in c.execute("SELECT job_type,status FROM jobs")
    ] == [("PURGE", "COMPLETED")]
    assert c.execute("SELECT COUNT(*) FROM current_entries").fetchone()[0] == 0
    assert c.execute("SELECT COUNT(*) FROM schema_migrations").fetchone()[0] == migrations
