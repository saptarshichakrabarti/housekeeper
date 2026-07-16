"""Small SQLite repository with explicit schema and parameterized queries."""

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .constants import SCHEMA_VERSION

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS scan_runs(id INTEGER PRIMARY KEY, source_root TEXT NOT NULL, source_root_fingerprint TEXT NOT NULL, started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at TEXT, status TEXT NOT NULL, hostname TEXT, platform TEXT, python_version TEXT, config_hash TEXT, files_seen INTEGER DEFAULT 0, directories_seen INTEGER DEFAULT 0, symlinks_seen INTEGER DEFAULT 0, errors_seen INTEGER DEFAULT 0, bytes_seen INTEGER DEFAULT 0, last_checkpoint_at TEXT);
CREATE TABLE IF NOT EXISTS filesystem_entries(id INTEGER PRIMARY KEY, scan_run_id INTEGER NOT NULL REFERENCES scan_runs(id), parent_entry_id INTEGER REFERENCES filesystem_entries(id), source_root TEXT NOT NULL, absolute_path TEXT NOT NULL, relative_path TEXT NOT NULL, name TEXT NOT NULL, suffix TEXT, entry_type TEXT NOT NULL, is_hidden INTEGER DEFAULT 0, is_symlink INTEGER DEFAULT 0, symlink_target TEXT, size_bytes INTEGER DEFAULT 0, device_id INTEGER, inode_or_file_id INTEGER, mode INTEGER, owner TEXT, group_name TEXT, created_at REAL, modified_at REAL, metadata_changed_at REAL, accessed_at REAL, birth_time_available INTEGER DEFAULT 0, scan_status TEXT, read_error TEXT, first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP, last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(scan_run_id, relative_path));
CREATE TABLE IF NOT EXISTS file_signatures(entry_id INTEGER PRIMARY KEY REFERENCES filesystem_entries(id) ON DELETE CASCADE, extension_mime TEXT, detected_mime TEXT, detected_type TEXT, signature_source TEXT, quick_hash TEXT, full_hash TEXT, hash_algorithm TEXT, hash_status TEXT, hash_error TEXT, full_hash_computed_at TEXT);
CREATE TABLE IF NOT EXISTS classifications(entry_id INTEGER PRIMARY KEY REFERENCES filesystem_entries(id) ON DELETE CASCADE, classification TEXT NOT NULL, confidence REAL, primary_reason_code TEXT, reason_codes_json TEXT, rule_ids_json TEXT, explanation TEXT, canonical_entry_id INTEGER, requires_manual_approval INTEGER, classified_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS analysis_jobs(id INTEGER PRIMARY KEY, job_type TEXT, started_at TEXT DEFAULT CURRENT_TIMESTAMP, completed_at TEXT, status TEXT, processed_count INTEGER DEFAULT 0, error_count INTEGER DEFAULT 0, config_hash TEXT, error_summary TEXT);
CREATE TABLE IF NOT EXISTS exact_duplicate_groups(id INTEGER PRIMARY KEY, full_hash TEXT NOT NULL, size_bytes INTEGER NOT NULL, member_count INTEGER NOT NULL, canonical_entry_id INTEGER, canonical_selection_reason TEXT, verified INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS exact_duplicate_members(group_id INTEGER REFERENCES exact_duplicate_groups(id) ON DELETE CASCADE, entry_id INTEGER REFERENCES filesystem_entries(id) ON DELETE CASCADE, is_canonical INTEGER, readable INTEGER, PRIMARY KEY(group_id,entry_id));
CREATE TABLE IF NOT EXISTS directory_summaries(entry_id INTEGER PRIMARY KEY REFERENCES filesystem_entries(id) ON DELETE CASCADE, recursive_file_count INTEGER, recursive_directory_count INTEGER, recursive_size_bytes INTEGER, unique_full_hash_count INTEGER, duplicate_file_count INTEGER, extension_distribution_json TEXT, earliest_modified_at REAL, latest_modified_at REAL, content_signature TEXT);
CREATE TABLE IF NOT EXISTS move_transactions(id INTEGER PRIMARY KEY, transaction_run_id TEXT, source_entry_id INTEGER, source_path TEXT, destination_path TEXT, expected_size INTEGER, expected_hash TEXT, pre_move_hash TEXT, post_move_hash TEXT, status TEXT, started_at TEXT DEFAULT CURRENT_TIMESTAMP, completed_at TEXT, error TEXT, restored_at TEXT, restore_status TEXT);
CREATE INDEX IF NOT EXISTS idx_entries_run ON filesystem_entries(scan_run_id); CREATE INDEX IF NOT EXISTS idx_entries_path ON filesystem_entries(relative_path); CREATE INDEX IF NOT EXISTS idx_entries_size ON filesystem_entries(size_bytes); CREATE INDEX IF NOT EXISTS idx_sig_full ON file_signatures(full_hash); CREATE INDEX IF NOT EXISTS idx_classification ON classifications(classification);
"""


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.conn: sqlite3.Connection | None = None

    def connect(self) -> sqlite3.Connection:
        if self.conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.conn = sqlite3.connect(self.path)
            self.conn.row_factory = sqlite3.Row
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA journal_mode=WAL")
        return self.conn

    def close(self) -> None:
        if self.conn:
            self.conn.close()
            self.conn = None

    def initialize(self) -> None:
        c = self.connect()
        c.executescript(SCHEMA)
        c.execute(
            "INSERT OR IGNORE INTO schema_migrations VALUES (?)", (SCHEMA_VERSION,)
        )
        c.commit()

    migrate = initialize

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        c = self.connect()
        try:
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.connect().execute(sql, params)

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        self.connect().executemany(sql, rows)
        self.connect().commit()

    def fetch_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.execute(sql, params).fetchone()

    def fetch_all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.execute(sql, params).fetchall()

    def create_scan_run(
        self, source_root: str, fingerprint: str, config_hash: str, **meta: str
    ) -> int:
        cur = self.connect().execute(
            "INSERT INTO scan_runs(source_root,source_root_fingerprint,status,config_hash,hostname,platform,python_version) VALUES(?,?, 'RUNNING',?,?,?,?)",
            (
                source_root,
                fingerprint,
                config_hash,
                meta.get("hostname"),
                meta.get("platform"),
                meta.get("python_version"),
            ),
        )
        self.connect().commit()
        return int(cur.lastrowid)

    def insert_entry(self, values: dict[str, Any]) -> int:
        keys = ",".join(values)
        marks = ",".join("?" for _ in values)
        cur = self.connect().execute(
            f"INSERT OR REPLACE INTO filesystem_entries({keys}) VALUES({marks})",
            tuple(values.values()),
        )
        return int(cur.lastrowid)
