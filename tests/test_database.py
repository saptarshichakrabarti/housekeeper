"""Database and migration tests: schema versioning, backups, integrity, legacy upgrades."""

import sqlite3

import pytest

from housekeeper.constants import SCHEMA_VERSION
from housekeeper.database import Database


def test_fresh_initialize_sets_schema_version(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    assert db.database_stats()["schema_version"] == SCHEMA_VERSION
    assert db.integrity_check() == "ok"


def test_initialize_is_idempotent(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    db.initialize()  # must not raise or duplicate schema rows
    assert db.database_stats()["schema_version"] == SCHEMA_VERSION


def test_content_object_is_deduplicated(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    first = db.get_or_create_content_object("sha256", "a" * 64, 10)
    second = db.get_or_create_content_object("sha256", "a" * 64, 10)
    assert first == second


def test_vacuum_adopts_the_larger_page_size_on_a_wal_database(tmp_path):
    """Regression: page_size must actually change on VACUUM, which SQLite ignores in WAL mode.

    A legacy database created at 4096 bytes/page, in WAL mode, is the exact case the previous code
    got wrong: ``PRAGMA page_size=8192; VACUUM`` is a silent no-op under WAL, so the file stayed at
    the old size. vacuum() now drops to a rollback journal for the rebuild and restores WAL after.
    """
    path = tmp_path / "legacy.sqlite"
    seed = sqlite3.connect(path)
    seed.execute("PRAGMA page_size=4096")
    seed.execute("PRAGMA journal_mode=WAL")
    seed.execute("CREATE TABLE t(x)")
    seed.executemany("INSERT INTO t(x) VALUES(?)", [(i,) for i in range(500)])
    seed.commit()
    seed.close()
    assert sqlite3.connect(path).execute("PRAGMA page_size").fetchone()[0] == 4096

    db = Database(path)
    db.vacuum()
    # The page size took, and the database is back in WAL mode with its data intact.
    assert db.connect().execute("PRAGMA page_size").fetchone()[0] == 8192
    assert db.connect().execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert db.connect().execute("SELECT COUNT(*) FROM t").fetchone()[0] == 500
    db.close()


def test_backup_refuses_overwrite_and_copies(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    out = tmp_path / "backup.sqlite"
    db.backup(out)
    assert out.exists()
    with pytest.raises(FileExistsError):
        db.backup(out)


def test_v1_database_migrates_and_backfills(tmp_path):
    old = tmp_path / "old.sqlite"
    conn = sqlite3.connect(old)
    conn.executescript(
        """CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY);
        INSERT INTO schema_migrations VALUES(1);
        CREATE TABLE scan_runs(id INTEGER PRIMARY KEY, source_root TEXT, source_root_fingerprint TEXT, status TEXT);
        CREATE TABLE filesystem_entries(id INTEGER PRIMARY KEY, scan_run_id INTEGER, entry_type TEXT, size_bytes INTEGER, relative_path TEXT, absolute_path TEXT, name TEXT, source_root TEXT);
        CREATE TABLE file_signatures(entry_id INTEGER PRIMARY KEY, full_hash TEXT, hash_algorithm TEXT, hash_status TEXT);
        INSERT INTO scan_runs VALUES(1,'/old','finger','COMPLETE');
        INSERT INTO filesystem_entries VALUES(1,1,'file',3,'a.txt','/old/a.txt','a.txt','/old');
        INSERT INTO file_signatures VALUES(1,'""" + "a" * 64 + """','sha256','OK');"""
    )
    conn.commit()
    conn.close()
    db = Database(old)
    db.initialize()
    assert db.database_stats()["schema_version"] == SCHEMA_VERSION
    # Legacy verified hash was backfilled into a content object + link.
    assert db.fetch_one("SELECT COUNT(*) n FROM content_objects")["n"] == 1
    assert db.fetch_one("SELECT COUNT(*) n FROM entry_content_links")["n"] == 1


def test_migration_is_resumable_and_rerunnable(tmp_path):
    old = tmp_path / "old.sqlite"
    conn = sqlite3.connect(old)
    conn.executescript(
        """CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY);
        INSERT INTO schema_migrations VALUES(1);
        CREATE TABLE scan_runs(id INTEGER PRIMARY KEY, source_root TEXT, source_root_fingerprint TEXT, status TEXT);
        CREATE TABLE filesystem_entries(id INTEGER PRIMARY KEY, scan_run_id INTEGER, entry_type TEXT, size_bytes INTEGER, relative_path TEXT, absolute_path TEXT, name TEXT, source_root TEXT);
        CREATE TABLE file_signatures(entry_id INTEGER PRIMARY KEY, full_hash TEXT, hash_algorithm TEXT, hash_status TEXT);"""
    )
    conn.commit()
    conn.close()
    Database(old).initialize()
    # Re-running the migration command must be safe and preserve the version.
    db = Database(old)
    db.initialize()
    assert db.database_stats()["schema_version"] == SCHEMA_VERSION


def test_read_only_connection_rejects_writes(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    with db.read_connection() as conn, pytest.raises(sqlite3.OperationalError):
        conn.execute("INSERT INTO scan_runs(source_root,source_root_fingerprint,status) VALUES('x','y','z')")
