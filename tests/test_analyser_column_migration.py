"""A database created before the British-spelling rename has analysis_artifacts.analyzer_* columns.

Opening it must self-heal (rename to analyser_*) so the dashboard/reports stop failing with
"no such column: analyser_name", and the *_metadata compat views must still work afterwards.
"""

import sqlite3

from housekeeper.database import Database

# Minimal pre-rename analysis_artifacts, exactly as old code created it (American column names).
_LEGACY = """
CREATE TABLE content_objects(id INTEGER PRIMARY KEY, hash_algorithm TEXT NOT NULL, full_hash TEXT NOT NULL, size_bytes INTEGER NOT NULL);
CREATE TABLE analysis_artifacts(id INTEGER PRIMARY KEY, content_object_id INTEGER NOT NULL,
  analyzer_name TEXT NOT NULL, analyzer_version TEXT NOT NULL, configuration_fingerprint TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL DEFAULT 'COMPLETED', started_at TEXT, completed_at TEXT, artifact_json TEXT,
  text_blob_id INTEGER, error_code TEXT, error_message TEXT,
  UNIQUE(content_object_id, analyzer_name, analyzer_version, configuration_fingerprint));
INSERT INTO content_objects(id,hash_algorithm,full_hash,size_bytes) VALUES(1,'sha256','abc',3);
INSERT INTO analysis_artifacts(content_object_id,analyzer_name,analyzer_version,status)
  VALUES(1,'documents','1','COMPLETED');
"""


def test_legacy_analyzer_columns_are_renamed_on_open(tmp_path):
    path = tmp_path / "legacy.sqlite"
    con = sqlite3.connect(path)
    con.executescript(_LEGACY)
    con.commit()
    con.close()

    db = Database(path)
    db.initialize()
    try:
        columns = {row[1] for row in db.connect().execute("PRAGMA table_info(analysis_artifacts)")}
        assert "analyser_name" in columns and "analyzer_name" not in columns
        assert "analyser_version" in columns and "analyzer_version" not in columns
        # The query that used to raise "no such column: analyser_name" now works and keeps the data.
        row = db.fetch_one(
            "SELECT analyser_name FROM analysis_artifacts WHERE analyser_name='documents'"
        )
        assert row is not None and row["analyser_name"] == "documents"
        # The dependent compat view is queryable again (it was recreated with the British column).
        db.fetch_all("SELECT * FROM document_metadata")
    finally:
        db.close()


def test_rename_is_a_noop_on_a_fresh_database(database):
    # A fresh (already-British) database must be untouched and re-initialisable without error.
    database.initialize()
    columns = {row[1] for row in database.connect().execute("PRAGMA table_info(analysis_artifacts)")}
    assert "analyser_name" in columns and "analyzer_name" not in columns
