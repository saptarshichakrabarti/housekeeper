"""Role-based canonical assignment tests, including the migration and survival constraint."""

import sqlite3

from housekeeper.analysers.exact_duplicates import run_exact_duplicate_analysis
from housekeeper.canonical.roles import (
    assign_canonical_roles,
    assign_location_roles,
    roles_for_group,
    roles_lost_if_moved,
)
from housekeeper.scanner import DriveScanner


def _dupe_group(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.bin").write_bytes(b"canonical-payload")
    (root / "b.bin").write_bytes(b"canonical-payload")
    DriveScanner(database, config).scan(root, incremental=False)
    run_exact_duplicate_analysis(database, config)
    return root


def test_location_role_assigned_for_exact_group(config, database, tmp_path):
    _dupe_group(config, database, tmp_path)
    assert assign_location_roles(database) == 1
    group = database.fetch_one("SELECT id,canonical_entry_id FROM exact_duplicate_groups")
    roles = roles_for_group(database, "EXACT_DUPLICATE_GROUP", group["id"])
    assert [r["canonical_role"] for r in roles] == ["CANONICAL_LOCATION"]
    assert roles[0]["entry_id"] == group["canonical_entry_id"]


def test_assignment_is_idempotent(config, database, tmp_path):
    _dupe_group(config, database, tmp_path)
    assign_canonical_roles(database)
    assign_canonical_roles(database)
    assert database.fetch_one(
        "SELECT COUNT(*) n FROM canonical_assignments WHERE canonical_role='CANONICAL_LOCATION'"
    )["n"] == 1


def test_survival_constraint_detects_role_loss(config, database, tmp_path):
    _dupe_group(config, database, tmp_path)
    assign_location_roles(database)
    row = database.fetch_one(
        "SELECT entry_id FROM canonical_assignments WHERE canonical_role='CANONICAL_LOCATION'"
    )
    canonical_entry = int(row["entry_id"])
    # Approving the only copy fulfilling CANONICAL_LOCATION would lose the role.
    lost = roles_lost_if_moved(database, {canonical_entry})
    assert any(item["role"] == "CANONICAL_LOCATION" for item in lost)
    # Approving an unrelated entry loses nothing.
    assert roles_lost_if_moved(database, {999_999}) == []


def test_v4_database_migration_backfills_canonical_location(tmp_path):
    old = tmp_path / "old.sqlite"
    conn = sqlite3.connect(old)
    conn.executescript(
        """CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY);
        INSERT INTO schema_migrations VALUES(4);
        CREATE TABLE scan_runs(id INTEGER PRIMARY KEY, source_root TEXT, source_root_fingerprint TEXT, status TEXT);
        CREATE TABLE filesystem_entries(id INTEGER PRIMARY KEY, scan_run_id INTEGER, entry_type TEXT, size_bytes INTEGER, relative_path TEXT, absolute_path TEXT, name TEXT, source_root TEXT);
        CREATE TABLE file_signatures(entry_id INTEGER PRIMARY KEY, full_hash TEXT, hash_algorithm TEXT, hash_status TEXT);
        CREATE TABLE content_objects(id INTEGER PRIMARY KEY, hash_algorithm TEXT, full_hash TEXT, size_bytes INTEGER, UNIQUE(hash_algorithm, full_hash, size_bytes));
        CREATE TABLE entry_content_links(entry_id INTEGER PRIMARY KEY, content_object_id INTEGER, link_status TEXT);
        CREATE TABLE exact_duplicate_groups(id INTEGER PRIMARY KEY, full_hash TEXT, size_bytes INTEGER, member_count INTEGER, canonical_entry_id INTEGER, canonical_selection_reason TEXT, verified INTEGER);
        INSERT INTO scan_runs VALUES(1,'/old','fp','COMPLETE');
        INSERT INTO filesystem_entries VALUES(1,1,'file',3,'a','/old/a','a','/old');
        INSERT INTO content_objects VALUES(1,'sha256','abc',3);
        INSERT INTO entry_content_links VALUES(1,1,'VERIFIED');
        INSERT INTO exact_duplicate_groups VALUES(1,'abc',3,2,1,'shortest',1);"""
    )
    conn.commit()
    conn.close()
    from housekeeper.database import Database

    db = Database(old)
    db.initialize()
    row = db.fetch_one(
        "SELECT canonical_role,entry_id FROM canonical_assignments WHERE target_group_type='EXACT_DUPLICATE_GROUP'"
    )
    assert row is not None
    assert row["canonical_role"] == "CANONICAL_LOCATION"
    assert row["entry_id"] == 1
