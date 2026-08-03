"""Small SQLite repository with explicit schema and parameterized queries."""

import json
import os
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any, ClassVar

from .constants import SCHEMA_VERSION
from .core import counters

SCHEMA = """
CREATE TABLE IF NOT EXISTS schema_migrations(version INTEGER PRIMARY KEY);
CREATE TABLE IF NOT EXISTS migration_progress(migration_version INTEGER PRIMARY KEY, cursor_value INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'PENDING', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, detail_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS scan_runs(id INTEGER PRIMARY KEY, source_root TEXT NOT NULL, source_root_fingerprint TEXT NOT NULL, started_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at TEXT, status TEXT NOT NULL, hostname TEXT, platform TEXT, python_version TEXT, config_hash TEXT, files_seen INTEGER DEFAULT 0, directories_seen INTEGER DEFAULT 0, symlinks_seen INTEGER DEFAULT 0, errors_seen INTEGER DEFAULT 0, bytes_seen INTEGER DEFAULT 0, last_checkpoint_at TEXT, frontier_json TEXT);
CREATE TABLE IF NOT EXISTS filesystem_entries(id INTEGER PRIMARY KEY, scan_run_id INTEGER NOT NULL REFERENCES scan_runs(id), parent_entry_id INTEGER REFERENCES filesystem_entries(id), source_root_id INTEGER REFERENCES source_roots(id), source_root TEXT NOT NULL, absolute_path TEXT NOT NULL, relative_path TEXT NOT NULL, name TEXT NOT NULL, suffix TEXT, entry_type TEXT NOT NULL, is_hidden INTEGER DEFAULT 0, is_symlink INTEGER DEFAULT 0, symlink_target TEXT, size_bytes INTEGER DEFAULT 0, device_id INTEGER, inode_or_file_id INTEGER, nlink INTEGER, mode INTEGER, owner TEXT, group_name TEXT, created_at REAL, modified_at REAL, metadata_changed_at REAL, accessed_at REAL, birth_time_available INTEGER DEFAULT 0, scan_status TEXT, read_error TEXT, first_seen_at TEXT DEFAULT CURRENT_TIMESTAMP, last_seen_at TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(scan_run_id, relative_path));
CREATE TABLE IF NOT EXISTS file_signatures(entry_id INTEGER PRIMARY KEY REFERENCES filesystem_entries(id) ON DELETE CASCADE, extension_mime TEXT, detected_mime TEXT, detected_type TEXT, signature_source TEXT, quick_hash TEXT, full_hash TEXT, hash_algorithm TEXT, hash_status TEXT, hash_error TEXT, full_hash_computed_at TEXT);
CREATE TABLE IF NOT EXISTS classifications(entry_id INTEGER PRIMARY KEY REFERENCES filesystem_entries(id) ON DELETE CASCADE, classification TEXT NOT NULL, confidence REAL, primary_reason_code TEXT, reason_codes_json TEXT, rule_ids_json TEXT, explanation TEXT, canonical_entry_id INTEGER, requires_manual_approval INTEGER, classified_at TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS analysis_jobs(id INTEGER PRIMARY KEY, job_type TEXT, started_at TEXT DEFAULT CURRENT_TIMESTAMP, completed_at TEXT, status TEXT, processed_count INTEGER DEFAULT 0, error_count INTEGER DEFAULT 0, config_hash TEXT, error_summary TEXT);
CREATE TABLE IF NOT EXISTS exact_duplicate_groups(id INTEGER PRIMARY KEY, content_object_id INTEGER REFERENCES content_objects(id), full_hash TEXT NOT NULL, size_bytes INTEGER NOT NULL, member_count INTEGER NOT NULL, canonical_entry_id INTEGER, canonical_selection_reason TEXT, verified INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS exact_duplicate_members(group_id INTEGER REFERENCES exact_duplicate_groups(id) ON DELETE CASCADE, entry_id INTEGER REFERENCES filesystem_entries(id) ON DELETE CASCADE, is_canonical INTEGER, readable INTEGER, PRIMARY KEY(group_id,entry_id));
CREATE TABLE IF NOT EXISTS directory_summaries(entry_id INTEGER PRIMARY KEY REFERENCES filesystem_entries(id) ON DELETE CASCADE, recursive_file_count INTEGER, recursive_directory_count INTEGER, recursive_size_bytes INTEGER, unique_full_hash_count INTEGER, duplicate_file_count INTEGER, extension_distribution_json TEXT, earliest_modified_at REAL, latest_modified_at REAL, content_signature TEXT);
CREATE TABLE IF NOT EXISTS move_transactions(id INTEGER PRIMARY KEY, transaction_run_id TEXT, source_entry_id INTEGER, source_path TEXT, destination_path TEXT, expected_size INTEGER, expected_hash TEXT, pre_move_hash TEXT, post_move_hash TEXT, status TEXT, started_at TEXT DEFAULT CURRENT_TIMESTAMP, completed_at TEXT, error TEXT, restored_at TEXT, restore_status TEXT);
CREATE INDEX IF NOT EXISTS idx_entries_path ON filesystem_entries(relative_path); CREATE INDEX IF NOT EXISTS idx_sig_full ON file_signatures(full_hash); CREATE INDEX IF NOT EXISTS idx_classification ON classifications(classification);
CREATE TABLE IF NOT EXISTS source_roots(id INTEGER PRIMARY KEY, display_name TEXT NOT NULL, source_fingerprint TEXT NOT NULL UNIQUE, filesystem_uuid TEXT, volume_label TEXT, device_metadata_json TEXT NOT NULL DEFAULT '{}', first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, last_mount_path TEXT NOT NULL, latest_complete_scan_run_id INTEGER REFERENCES scan_runs(id));
CREATE TABLE IF NOT EXISTS content_objects(id INTEGER PRIMARY KEY, hash_algorithm TEXT NOT NULL, full_hash TEXT NOT NULL, size_bytes INTEGER NOT NULL, first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, last_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, verification_status TEXT NOT NULL DEFAULT 'VERIFIED', readability_status TEXT NOT NULL DEFAULT 'UNKNOWN', content_kind TEXT, detected_mime TEXT, detected_type TEXT, analysis_state TEXT NOT NULL DEFAULT 'PENDING', created_by_scan_run_id INTEGER REFERENCES scan_runs(id), UNIQUE(hash_algorithm, full_hash, size_bytes));
CREATE TABLE IF NOT EXISTS entry_content_links(entry_id INTEGER PRIMARY KEY REFERENCES filesystem_entries(id) ON DELETE CASCADE, content_object_id INTEGER NOT NULL REFERENCES content_objects(id), linked_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, link_status TEXT NOT NULL, size_verified INTEGER NOT NULL DEFAULT 0, hash_verified INTEGER NOT NULL DEFAULT 0, entry_stat_fingerprint TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS analysis_artifacts(id INTEGER PRIMARY KEY, content_object_id INTEGER NOT NULL REFERENCES content_objects(id) ON DELETE CASCADE, analyser_name TEXT NOT NULL, analyser_version TEXT NOT NULL, configuration_fingerprint TEXT NOT NULL, status TEXT NOT NULL, started_at TEXT, completed_at TEXT, artifact_json TEXT, text_blob_id INTEGER, error_code TEXT, error_message TEXT, UNIQUE(content_object_id, analyser_name, analyser_version, configuration_fingerprint));
CREATE TABLE IF NOT EXISTS content_text_blobs(id INTEGER PRIMARY KEY, content_object_id INTEGER NOT NULL REFERENCES content_objects(id) ON DELETE CASCADE, text_kind TEXT NOT NULL, compression TEXT NOT NULL DEFAULT 'none', character_count INTEGER NOT NULL, text_hash TEXT NOT NULL, data BLOB NOT NULL, UNIQUE(content_object_id, text_kind, text_hash));
CREATE TABLE IF NOT EXISTS scan_entry_changes(id INTEGER PRIMARY KEY, scan_run_id INTEGER NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE, entry_id INTEGER REFERENCES filesystem_entries(id) ON DELETE CASCADE, relative_path TEXT NOT NULL, change_status TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS jobs(id INTEGER PRIMARY KEY, job_type TEXT NOT NULL, scope_json TEXT NOT NULL DEFAULT '{}', configuration_fingerprint TEXT NOT NULL DEFAULT '', status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, started_at TEXT, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, completed_at TEXT, processed_count INTEGER NOT NULL DEFAULT 0, total_estimate INTEGER, success_count INTEGER NOT NULL DEFAULT 0, skip_count INTEGER NOT NULL DEFAULT 0, error_count INTEGER NOT NULL DEFAULT 0, current_item TEXT, checkpoint_json TEXT NOT NULL DEFAULT '{}', worker_count INTEGER NOT NULL DEFAULT 1, host TEXT, process_id INTEGER, parent_job_id INTEGER REFERENCES jobs(id));
CREATE TABLE IF NOT EXISTS review_sessions(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, description TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, status TEXT NOT NULL DEFAULT 'OPEN', base_scan_run_id INTEGER, policy_version TEXT NOT NULL DEFAULT '1', analysis_snapshot_id TEXT, filter_json TEXT NOT NULL DEFAULT '{}');
CREATE TABLE IF NOT EXISTS review_decisions(id INTEGER PRIMARY KEY, review_session_id INTEGER NOT NULL REFERENCES review_sessions(id), target_type TEXT NOT NULL, target_id INTEGER NOT NULL, decision TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, current INTEGER NOT NULL DEFAULT 1, user_note TEXT NOT NULL DEFAULT '', reason TEXT NOT NULL DEFAULT '', source TEXT NOT NULL DEFAULT 'cli', stale INTEGER NOT NULL DEFAULT 0, UNIQUE(review_session_id,target_type,target_id,current));
CREATE TABLE IF NOT EXISTS review_decision_history(id INTEGER PRIMARY KEY, decision_id INTEGER, review_session_id INTEGER NOT NULL, target_type TEXT NOT NULL, target_id INTEGER NOT NULL, previous_decision TEXT, new_decision TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, user_note TEXT NOT NULL DEFAULT '', analysis_snapshot_id TEXT, source TEXT NOT NULL DEFAULT 'cli');
CREATE TABLE IF NOT EXISTS review_snapshots(id INTEGER PRIMARY KEY, review_session_id INTEGER NOT NULL REFERENCES review_sessions(id), snapshot_json TEXT NOT NULL, manifest_hash TEXT NOT NULL UNIQUE, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS relationships(id INTEGER PRIMARY KEY, source_type TEXT NOT NULL, source_id INTEGER NOT NULL, target_type TEXT NOT NULL, target_id INTEGER NOT NULL, relationship_type TEXT NOT NULL, confidence REAL NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}', relationship_version TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(source_type,source_id,target_type,target_id,relationship_type,relationship_version));
CREATE TABLE IF NOT EXISTS relationship_groups(id INTEGER PRIMARY KEY, group_type TEXT NOT NULL, group_key TEXT NOT NULL, relationship_version TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(group_type,group_key,relationship_version));
CREATE TABLE IF NOT EXISTS relationship_group_members(group_id INTEGER NOT NULL REFERENCES relationship_groups(id) ON DELETE CASCADE, content_object_id INTEGER NOT NULL REFERENCES content_objects(id) ON DELETE CASCADE, role TEXT NOT NULL DEFAULT 'MEMBER', evidence_json TEXT NOT NULL DEFAULT '{}', PRIMARY KEY(group_id,content_object_id));
CREATE TABLE IF NOT EXISTS graph_layout_cache(cache_key TEXT PRIMARY KEY, projection_json TEXT NOT NULL, layout_json TEXT NOT NULL, relationship_version TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS projects(id INTEGER PRIMARY KEY, root_entry_id INTEGER REFERENCES filesystem_entries(id), name TEXT NOT NULL, kind TEXT NOT NULL, markers_json TEXT NOT NULL DEFAULT '[]', source_size_bytes INTEGER NOT NULL DEFAULT 0, generated_size_bytes INTEGER NOT NULL DEFAULT 0, environment_size_bytes INTEGER NOT NULL DEFAULT 0, git_status TEXT, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(root_entry_id));
CREATE TABLE IF NOT EXISTS canonical_overrides(id INTEGER PRIMARY KEY, duplicate_group_id INTEGER NOT NULL REFERENCES exact_duplicate_groups(id), canonical_entry_id INTEGER NOT NULL REFERENCES filesystem_entries(id), review_session_id INTEGER REFERENCES review_sessions(id), evidence_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(duplicate_group_id));
-- Contact-sheet reuse keys. A rendered sheet is a pure function of its member ids and their
-- thumbnails, so an unchanged key means the render would reproduce the file already on disk.
-- Keyed by group and cascaded from it: replace_relationship_group deletes and reinserts groups,
-- and a reuse key that outlived its group would authorise reusing a sheet for different members.
CREATE TABLE IF NOT EXISTS contact_sheet_renders(group_id INTEGER PRIMARY KEY REFERENCES relationship_groups(id) ON DELETE CASCADE, input_key TEXT NOT NULL, rendered_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS materialized_summaries(summary_key TEXT PRIMARY KEY, value_json TEXT NOT NULL, source_scan_run_id INTEGER REFERENCES scan_runs(id), refreshed_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE INDEX IF NOT EXISTS idx_link_content ON entry_content_links(content_object_id); CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status,updated_at); CREATE INDEX IF NOT EXISTS idx_review_queue ON review_decisions(review_session_id,current,decision,stale); CREATE INDEX IF NOT EXISTS idx_relationship_source ON relationships(source_type,source_id,relationship_type); CREATE INDEX IF NOT EXISTS idx_relationship_target ON relationships(target_type,target_id,relationship_type); CREATE INDEX IF NOT EXISTS idx_relationship_group_members_content ON relationship_group_members(content_object_id,group_id);
CREATE INDEX IF NOT EXISTS idx_changes_run_status ON scan_entry_changes(scan_run_id,change_status); CREATE INDEX IF NOT EXISTS idx_changes_entry ON scan_entry_changes(entry_id,id); CREATE INDEX IF NOT EXISTS idx_artifacts_name_status ON analysis_artifacts(analyser_name,status,completed_at);
-- Hot-path indexes for dashboard review/overview queries (see database.refresh_materialized_summaries and DashboardService).
-- The filesystem_entries(suffix,...) indexes are created in initialize() *after* legacy columns are added, since `suffix` is one of them.
CREATE INDEX IF NOT EXISTS idx_review_decisions_target ON review_decisions(target_type,target_id,current); CREATE INDEX IF NOT EXISTS idx_dupe_members_entry ON exact_duplicate_members(entry_id);
CREATE TABLE IF NOT EXISTS normalization_profiles(id INTEGER PRIMARY KEY, name TEXT NOT NULL, content_kind TEXT NOT NULL, algorithm TEXT NOT NULL, algorithm_version TEXT NOT NULL, configuration_json TEXT NOT NULL DEFAULT '{}', configuration_fingerprint TEXT NOT NULL, loss_characteristics_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, deprecated_at TEXT, UNIQUE(name, algorithm_version, configuration_fingerprint));
CREATE TABLE IF NOT EXISTS normalized_content_artifacts(id INTEGER PRIMARY KEY, content_object_id INTEGER NOT NULL REFERENCES content_objects(id) ON DELETE CASCADE, normalization_profile_id INTEGER NOT NULL REFERENCES normalization_profiles(id), status TEXT NOT NULL, normalized_hash TEXT, normalized_size_bytes INTEGER, structural_fingerprint TEXT, artifact_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, error_code TEXT, error_message TEXT, UNIQUE(content_object_id, normalization_profile_id));
CREATE TABLE IF NOT EXISTS content_relationships(id INTEGER PRIMARY KEY, source_type TEXT NOT NULL, source_id INTEGER NOT NULL, target_type TEXT NOT NULL, target_id INTEGER NOT NULL, relationship_type TEXT NOT NULL, evidence_tier TEXT NOT NULL, confidence REAL NOT NULL, algorithm TEXT NOT NULL, algorithm_version TEXT NOT NULL, configuration_fingerprint TEXT NOT NULL DEFAULT '', evidence_json TEXT NOT NULL DEFAULT '{}', explanation TEXT NOT NULL DEFAULT '', status TEXT NOT NULL DEFAULT 'ACTIVE', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, invalidated_at TEXT, UNIQUE(source_type,source_id,target_type,target_id,relationship_type,algorithm,algorithm_version,configuration_fingerprint));
CREATE TABLE IF NOT EXISTS similarity_signatures(id INTEGER PRIMARY KEY, content_object_id INTEGER NOT NULL REFERENCES content_objects(id) ON DELETE CASCADE, signature_type TEXT NOT NULL, signature_version TEXT NOT NULL, configuration_fingerprint TEXT NOT NULL DEFAULT '', signature_blob TEXT, feature_count INTEGER, status TEXT NOT NULL DEFAULT 'OK', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(content_object_id, signature_type, signature_version, configuration_fingerprint));
CREATE TABLE IF NOT EXISTS canonical_assignments(id INTEGER PRIMARY KEY, target_group_type TEXT NOT NULL, target_group_id INTEGER NOT NULL, canonical_role TEXT NOT NULL, entry_id INTEGER REFERENCES filesystem_entries(id), content_object_id INTEGER REFERENCES content_objects(id), score REAL, score_components_json TEXT NOT NULL DEFAULT '{}', source TEXT NOT NULL DEFAULT 'analyser', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, superseded_at TEXT, UNIQUE(target_group_type,target_group_id,canonical_role,entry_id));
CREATE INDEX IF NOT EXISTS idx_content_rel_source ON content_relationships(source_type,source_id,relationship_type,status); CREATE INDEX IF NOT EXISTS idx_content_rel_target ON content_relationships(target_type,target_id,relationship_type,status); CREATE INDEX IF NOT EXISTS idx_content_rel_tier ON content_relationships(evidence_tier,status);
CREATE INDEX IF NOT EXISTS idx_norm_artifact_hash ON normalized_content_artifacts(normalization_profile_id,normalized_hash); CREATE INDEX IF NOT EXISTS idx_sig_lookup ON similarity_signatures(signature_type,signature_version); CREATE INDEX IF NOT EXISTS idx_canonical_group ON canonical_assignments(target_group_type,target_group_id,canonical_role);
-- Banded LSH index over the 64-bit image descriptor: 9 bands make bucket equality complete for
-- Hamming radius 8 (see analysers/images.py). Derived from analysis_artifacts and rebuilt by
-- anti-join, so it is safe to drop. No secondary index: the analyser reads the whole table in
-- bucket order, and EXPLAIN shows the planner takes the primary key either way.
CREATE TABLE IF NOT EXISTS image_phash_bands(content_object_id INTEGER NOT NULL REFERENCES content_objects(id) ON DELETE CASCADE, band_index INTEGER NOT NULL, band_value INTEGER NOT NULL, phash TEXT NOT NULL, PRIMARY KEY(content_object_id,band_index)) WITHOUT ROWID;
CREATE TABLE IF NOT EXISTS chunk_profiles(id INTEGER PRIMARY KEY, name TEXT NOT NULL, algorithm TEXT NOT NULL, algorithm_version TEXT NOT NULL, minimum_chunk_size INTEGER NOT NULL, average_chunk_size INTEGER NOT NULL, maximum_chunk_size INTEGER NOT NULL, hash_algorithm TEXT NOT NULL, configuration_fingerprint TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(name, algorithm_version, configuration_fingerprint));
CREATE TABLE IF NOT EXISTS content_chunks(id INTEGER PRIMARY KEY, chunking_profile_id INTEGER NOT NULL REFERENCES chunk_profiles(id), chunk_hash_algorithm TEXT NOT NULL, chunk_hash TEXT NOT NULL, size_bytes INTEGER NOT NULL, occurrence_count INTEGER NOT NULL DEFAULT 0, first_seen_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(chunking_profile_id, chunk_hash_algorithm, chunk_hash, size_bytes));
CREATE TABLE IF NOT EXISTS chunk_occurrences(content_object_id INTEGER NOT NULL REFERENCES content_objects(id) ON DELETE CASCADE, chunk_id INTEGER NOT NULL REFERENCES content_chunks(id) ON DELETE CASCADE, sequence_index INTEGER NOT NULL, byte_offset INTEGER NOT NULL, size_bytes INTEGER NOT NULL, PRIMARY KEY(content_object_id, sequence_index));
CREATE TABLE IF NOT EXISTS content_overlap_results(id INTEGER PRIMARY KEY, content_object_a_id INTEGER NOT NULL, content_object_b_id INTEGER NOT NULL, chunking_profile_id INTEGER NOT NULL, shared_chunk_count INTEGER NOT NULL, shared_chunk_bytes INTEGER NOT NULL, a_total_chunk_bytes INTEGER NOT NULL, b_total_chunk_bytes INTEGER NOT NULL, overlap_a_in_b REAL NOT NULL, overlap_b_in_a REAL NOT NULL, weighted_jaccard REAL NOT NULL, ordered_overlap_score REAL NOT NULL DEFAULT 0, confidence REAL NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(content_object_a_id, content_object_b_id, chunking_profile_id));
CREATE TABLE IF NOT EXISTS collection_clusters(id INTEGER PRIMARY KEY, cluster_type TEXT NOT NULL, name TEXT NOT NULL, confidence REAL NOT NULL DEFAULT 1.0, algorithm TEXT NOT NULL DEFAULT '', algorithm_version TEXT NOT NULL DEFAULT '1', scope_json TEXT NOT NULL DEFAULT '{}', summary_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(cluster_type, name));
CREATE TABLE IF NOT EXISTS collection_members(cluster_id INTEGER NOT NULL REFERENCES collection_clusters(id) ON DELETE CASCADE, member_type TEXT NOT NULL, member_id INTEGER NOT NULL, membership_confidence REAL NOT NULL DEFAULT 1.0, membership_evidence_json TEXT NOT NULL DEFAULT '{}', sequence_index INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(cluster_id, member_type, member_id));
CREATE TABLE IF NOT EXISTS retention_policies(id INTEGER PRIMARY KEY, name TEXT NOT NULL, version TEXT NOT NULL DEFAULT '1', description TEXT NOT NULL DEFAULT '', rules_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, deprecated_at TEXT, UNIQUE(name, version));
CREATE TABLE IF NOT EXISTS record_series(id INTEGER PRIMARY KEY, name TEXT NOT NULL UNIQUE, description TEXT NOT NULL DEFAULT '', parent_series_id INTEGER REFERENCES record_series(id), retention_policy_id INTEGER REFERENCES retention_policies(id), sensitivity TEXT NOT NULL DEFAULT 'normal', default_preservation_priority INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS record_series_assignments(id INTEGER PRIMARY KEY, target_type TEXT NOT NULL, target_id INTEGER NOT NULL, series_id INTEGER NOT NULL REFERENCES record_series(id), confidence REAL NOT NULL DEFAULT 1.0, evidence_json TEXT NOT NULL DEFAULT '{}', source TEXT NOT NULL DEFAULT 'analyser', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(target_type, target_id, series_id));
CREATE TABLE IF NOT EXISTS preservation_assessments(id INTEGER PRIMARY KEY, target_type TEXT NOT NULL, target_id INTEGER NOT NULL, format_risk TEXT NOT NULL DEFAULT 'none', integrity_risk TEXT NOT NULL DEFAULT 'none', context_loss_risk TEXT NOT NULL DEFAULT 'none', accessibility_risk TEXT NOT NULL DEFAULT 'none', encryption_risk TEXT NOT NULL DEFAULT 'none', application_dependency_risk TEXT NOT NULL DEFAULT 'none', recommended_action TEXT NOT NULL DEFAULT 'KEEP_WITH_CHECKSUM', evidence_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(target_type, target_id));
CREATE TABLE IF NOT EXISTS known_content_assertions(id INTEGER PRIMARY KEY, assertion TEXT NOT NULL, scope_type TEXT NOT NULL, scope_value TEXT NOT NULL, evidence_json TEXT NOT NULL DEFAULT '{}', source TEXT NOT NULL DEFAULT 'user', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, review_at TEXT, expires_at TEXT, UNIQUE(assertion, scope_type, scope_value));
CREATE TABLE IF NOT EXISTS entry_lifecycle(entry_id INTEGER PRIMARY KEY REFERENCES filesystem_entries(id) ON DELETE CASCADE, state TEXT NOT NULL, recommendation TEXT NOT NULL DEFAULT '', evidence_json TEXT NOT NULL DEFAULT '{}', updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS review_priority(id INTEGER PRIMARY KEY, target_type TEXT NOT NULL, target_id INTEGER NOT NULL, category TEXT NOT NULL, score REAL NOT NULL, components_json TEXT NOT NULL DEFAULT '{}', explanation TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE(target_type, target_id));
CREATE TABLE IF NOT EXISTS review_learning_models(id INTEGER PRIMARY KEY, model_type TEXT NOT NULL, model_version TEXT NOT NULL, feature_schema_version TEXT NOT NULL, training_scope_json TEXT NOT NULL DEFAULT '{}', training_count INTEGER NOT NULL DEFAULT 0, metrics_json TEXT NOT NULL DEFAULT '{}', artifact_json TEXT NOT NULL DEFAULT '{}', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, active INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS review_learning_predictions(id INTEGER PRIMARY KEY, model_id INTEGER NOT NULL REFERENCES review_learning_models(id) ON DELETE CASCADE, target_type TEXT NOT NULL, target_id INTEGER NOT NULL, predicted_decision TEXT NOT NULL, probability REAL NOT NULL, feature_summary_json TEXT NOT NULL DEFAULT '{}', precedent_examples_json TEXT NOT NULL DEFAULT '[]', created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, stale INTEGER NOT NULL DEFAULT 0, UNIQUE(model_id, target_type, target_id));
CREATE INDEX IF NOT EXISTS idx_chunk_occ_chunk ON chunk_occurrences(chunk_id); CREATE INDEX IF NOT EXISTS idx_chunk_hash ON content_chunks(chunk_hash); CREATE INDEX IF NOT EXISTS idx_overlap_a ON content_overlap_results(content_object_a_id); CREATE INDEX IF NOT EXISTS idx_collection_member ON collection_members(member_type,member_id); CREATE INDEX IF NOT EXISTS idx_series_assign ON record_series_assignments(target_type,target_id); CREATE INDEX IF NOT EXISTS idx_preservation_target ON preservation_assessments(target_type,target_id); CREATE INDEX IF NOT EXISTS idx_priority_cat ON review_priority(category,score);
-- Compatibility views: the original spec named per-format metadata and relationship-group tables.
-- Their data is stored normalized (analysis_artifacts / relationships / relationship_groups); these
-- views expose it under the spec's table names so `SELECT * FROM document_metadata` etc. works.
CREATE VIEW IF NOT EXISTS document_metadata AS
  SELECT l.entry_id AS entry_id, a.content_object_id AS content_object_id,
    COALESCE(json_extract(a.artifact_json,'$.structured_metadata.document_type'),'text') AS document_kind,
    json_extract(a.artifact_json,'$.normalized_text_hash') AS normalized_text_hash,
    json_extract(a.artifact_json,'$.character_count') AS character_count,
    json_extract(a.artifact_json,'$.word_count') AS word_count,
    a.status AS extraction_status, a.error_message AS extraction_error
  FROM analysis_artifacts a JOIN entry_content_links l ON l.content_object_id=a.content_object_id
  WHERE a.analyser_name='documents';
CREATE VIEW IF NOT EXISTS image_metadata AS
  SELECT l.entry_id AS entry_id, a.content_object_id AS content_object_id,
    json_extract(a.artifact_json,'$.format') AS format,
    json_extract(a.artifact_json,'$.width') AS width, json_extract(a.artifact_json,'$.height') AS height,
    json_extract(a.artifact_json,'$.perceptual_hash') AS perceptual_hash, a.status AS analysis_status
  FROM analysis_artifacts a JOIN entry_content_links l ON l.content_object_id=a.content_object_id
  WHERE a.analyser_name='images';
CREATE VIEW IF NOT EXISTS media_metadata AS
  SELECT l.entry_id AS entry_id, a.content_object_id AS content_object_id,
    json_extract(a.artifact_json,'$.media_kind') AS media_kind,
    json_extract(a.artifact_json,'$.duration_seconds') AS duration_seconds,
    json_extract(a.artifact_json,'$.bitrate') AS bitrate, json_extract(a.artifact_json,'$.codec') AS codec,
    a.status AS analysis_status
  FROM analysis_artifacts a JOIN entry_content_links l ON l.content_object_id=a.content_object_id
  WHERE a.analyser_name='media';
CREATE VIEW IF NOT EXISTS archive_metadata AS
  SELECT l.entry_id AS entry_id, a.content_object_id AS content_object_id,
    json_extract(a.artifact_json,'$.archive_kind') AS archive_kind,
    json_extract(a.artifact_json,'$.member_count') AS member_count,
    json_extract(a.artifact_json,'$.manifest_hash') AS manifest_hash,
    json_extract(a.artifact_json,'$.nested_archive_count') AS nested_archive_count, a.status AS analysis_status
  FROM analysis_artifacts a JOIN entry_content_links l ON l.content_object_id=a.content_object_id
  WHERE a.analyser_name='archives';
CREATE VIEW IF NOT EXISTS directory_overlap_results AS
  SELECT id, source_id AS directory_a_id, target_id AS directory_b_id,
    json_extract(evidence_json,'$.shared_hashes') AS shared_file_hashes,
    json_extract(evidence_json,'$.source_hashes') AS a_file_hashes,
    json_extract(evidence_json,'$.target_hashes') AS b_file_hashes,
    confidence AS containment_a_in_b, confidence AS containment_b_in_a, created_at
  FROM relationships WHERE relationship_type='MOSTLY_CONTAINED_IN';
CREATE VIEW IF NOT EXISTS document_version_groups AS
  SELECT id, group_key AS normalized_family_name, 1.0 AS group_confidence, 1 AS review_required, created_at
  FROM relationship_groups WHERE group_type='DOCUMENT_FAMILY';
CREATE VIEW IF NOT EXISTS document_version_members AS
  SELECT g.id AS group_id, m.content_object_id AS entry_id, 0 AS sequence_index, m.role AS relationship_type
  FROM relationship_group_members m JOIN relationship_groups g ON g.id=m.group_id WHERE g.group_type='DOCUMENT_FAMILY';
CREATE VIEW IF NOT EXISTS image_similarity_groups AS
  SELECT id, 'PERCEPTUAL' AS group_kind, 1.0 AS group_confidence, 1 AS review_required, created_at
  FROM relationship_groups WHERE group_type='IMAGE_SIMILARITY';
CREATE VIEW IF NOT EXISTS image_similarity_members AS
  SELECT g.id AS group_id, m.content_object_id AS entry_id, 0 AS distance_score, 0 AS is_representative
  FROM relationship_group_members m JOIN relationship_groups g ON g.id=m.group_id WHERE g.group_type='IMAGE_SIMILARITY';
"""


def _keyset_pages(execute, sql, params, key_exprs, key_of, batch_size):
    """Stream ``sql`` in keyset pages, closing the cursor between pages.

    A single ``execute``+``fetchmany`` loop holds one read snapshot for its whole lifetime, and in
    WAL that snapshot pins the log: a checkpoint cannot reclaim any frame newer than it, so on a
    days-long stage the WAL grows to the size of everything the writer commits meanwhile. Paging by
    a keyset — fetch a bounded page, close the cursor (releasing the snapshot), re-open past the last
    key — lets a checkpoint advance between pages, so the WAL stays bounded.

    ``sql`` must contain the literal ``{keyset}`` in its ``WHERE`` and carry no ``ORDER BY``/``LIMIT``
    of its own (both are appended here). ``key_exprs`` are the ordering/comparison expressions and
    ``key_of(row)`` returns the matching boundary tuple. The comparison is a row-value ``>``, so the
    ordering must be ascending on exactly ``key_exprs``. Each page sees a *fresh* snapshot; for the
    anti-join candidate queries this only ever removes rows already processed, which is correct.
    """
    order = ",".join(key_exprs)
    cols = "(" + ",".join(key_exprs) + ")"
    marks = "(" + ",".join("?" for _ in key_exprs) + ")"
    boundary = None
    while True:
        if boundary is None:
            query = sql.format(keyset="") + f" ORDER BY {order} LIMIT ?"
            page_params = (*params, batch_size)
        else:
            query = sql.format(keyset=f" AND {cols} > {marks}") + f" ORDER BY {order} LIMIT ?"
            page_params = (*params, *boundary, batch_size)
        cursor = execute(query, page_params)
        rows = cursor.fetchall()
        cursor.close()
        if not rows:
            return
        yield from rows
        if len(rows) < batch_size:
            return
        boundary = key_of(rows[-1])


class Database:
    # The insert path ran on SQLite's 2 MB default page cache while writing a multi-GB inventory,
    # so every B-tree descent above that went back to the OS. Cache is allocated lazily, so a
    # 256 MB ceiling costs nothing on a small database. wal_autocheckpoint is raised because a
    # bulk scan otherwise checkpoints every ~4 MB of WAL, mid-insert.
    _WRITE_PRAGMAS = (
        "PRAGMA cache_size=-262144",
        "PRAGMA mmap_size=268435456",
        "PRAGMA temp_store=MEMORY",
        "PRAGMA wal_autocheckpoint=4000",
    )

    def __init__(self, path: Path):
        self.path = Path(path)
        self.conn: sqlite3.Connection | None = None
        self._lock = threading.RLock()
        # Read-only connections are thread-local: WAL supports many concurrent readers, so each
        # dashboard worker thread gets its own connection that never contends with the writer or
        # with sibling readers. Never shared across threads, so check_same_thread can stay on.
        self._local = threading.local()

    def connect(self) -> sqlite3.Connection:
        if self.conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # A larger page shallows the B-trees of a multi-hundred-GB inventory, so a lookup touches
            # fewer pages. It can only be chosen while the database is empty (before any page is
            # allocated) and before WAL is enabled — an existing file keeps its page size until a
            # VACUUM. Checked before connect(), which itself creates the file.
            fresh = not self.path.exists() or self.path.stat().st_size == 0
            self.conn = sqlite3.connect(
                self.path, check_same_thread=False, factory=counters.Connection
            )
            self.conn.row_factory = sqlite3.Row
            if fresh:
                self.conn.execute("PRAGMA page_size=8192")
            self.conn.execute("PRAGMA foreign_keys=ON")
            self.conn.execute("PRAGMA journal_mode=WAL")
            self.conn.execute("PRAGMA busy_timeout=5000")
            self.conn.execute("PRAGMA synchronous=NORMAL")
            for pragma in self._WRITE_PRAGMAS:
                self.conn.execute(pragma)
        return self.conn

    def close(self) -> None:
        if self.conn:
            self.conn.close()
            self.conn = None

    def initialize(self) -> None:
        c = self.connect()
        # Heal legacy British-spelling columns *before* SCHEMA runs: SCHEMA (re)creates British-named
        # indexes/views on analysis_artifacts, which would fail on a database whose column is still
        # ``analyzer_name`` and which lacks those objects.
        self._rename_analyser_columns(c)
        c.executescript(SCHEMA)
        self._ensure_legacy_columns(c)
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_entries_source_root ON filesystem_entries(source_root_id,id)"
        )
        # Dashboard hot-path indexes on `suffix` — created here (not in SCHEMA) because `suffix` is
        # one of the legacy columns just backfilled above, so it may not exist when SCHEMA runs.
        #
        # Run-leading, and that ordering is the whole point. The predecessor was
        # (entry_type,suffix,size_bytes), sized for a chart that aggregated every snapshot ever
        # recorded. Now that the chart reads current_entries, that index answers it as a covering
        # scan over all history and then discards most of it — one statement, but a cost that grows
        # with every rescan. Leading with scan_run_id makes the same aggregate a bounded seek.
        c.execute("DROP INDEX IF EXISTS idx_entries_type_suffix_size")
        c.execute(
            "CREATE INDEX IF NOT EXISTS idx_entries_run_type_suffix_size ON filesystem_entries(scan_run_id,entry_type,suffix,size_bytes)"
        )
        c.execute("CREATE INDEX IF NOT EXISTS idx_entries_suffix ON filesystem_entries(suffix)")
        self._ensure_entry_indexes(c)
        c.execute(
            """INSERT OR IGNORE INTO source_roots(display_name,source_fingerprint,last_mount_path)
               SELECT COALESCE(NULLIF(MAX(source_root),''),'legacy source'),source_root_fingerprint,
                      COALESCE(NULLIF(MAX(source_root),''),'')
               FROM scan_runs GROUP BY source_root_fingerprint"""
        )
        c.execute(
            """UPDATE filesystem_entries SET source_root_id=(
               SELECT sr.id FROM scan_runs run JOIN source_roots sr ON sr.source_fingerprint=run.source_root_fingerprint
               WHERE run.id=filesystem_entries.scan_run_id LIMIT 1)
               WHERE source_root_id IS NULL"""
        )
        versions = [
            r[0] for r in c.execute("SELECT version FROM schema_migrations ORDER BY version")
        ]
        if not versions:
            c.execute("INSERT INTO schema_migrations(version) VALUES (?)", (SCHEMA_VERSION,))
            c.execute(
                "INSERT OR REPLACE INTO migration_progress(migration_version,cursor_value,status,detail_json) VALUES(4,0,'COMPLETED','{\"fresh_database\":true}')"
            )
        elif max(versions) < SCHEMA_VERSION:
            integrity = c.execute("PRAGMA integrity_check").fetchone()[0]
            if integrity != "ok":
                raise sqlite3.DatabaseError(
                    f"refusing migration: integrity_check returned {integrity}"
                )
            if max(versions) < 2:
                self._migrate_v1_to_v2(c)
            if max(versions) < 3:
                c.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (3)")
            if max(versions) < 4:
                self._migrate_v3_to_v4(c)
            if max(versions) < 5:
                self._migrate_v4_to_v5(c)
            if max(versions) < 6:
                c.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (6)")
            if max(versions) < 7:
                self._migrate_v6_to_v7(c)
            if max(versions) < 8:
                self._migrate_v7_to_v8(c)
            if max(versions) < 9:
                self._migrate_v8_to_v9(c)
        # After the migrations: the backfill above must settle before the index can be unique.
        self._ensure_duplicate_group_identity(c)
        self._ensure_change_identity(c)
        self.refresh_current_inventory_views()
        c.commit()

    def refresh_current_inventory_views(self) -> frozenset[int]:
        """(Re)define the ``current_*`` relational layer — the drive as it is now.

        Historical base rows are the audit trail and are deliberately retained. Every current-state
        consumer reads these views instead. Scoping by *naming the right relation* rather than by
        remembering to pass a parameter is the point: doing this only for entries/classifications
        fixed repeated-scan double counting but still let retired duplicate groups, projects,
        artifacts and relationships leak into reports after every current member was deleted.

        The run ids are baked in as literals. The obvious alternative —
        ``scan_run_id IN (SELECT latest_complete_scan_run_id FROM source_roots …)`` — plans as
        ``SCAN filesystem_entries`` with a LIST SUBQUERY and post-filters, measured 4–15× slower
        than this form on a 20-snapshot corpus and *slower than not scoping at all*. That is the
        same defect the composite indexes above exist to prevent, so the view is refreshed in the
        one transaction that moves the pointer instead.
        """
        c = self.connect()
        # The scanner's pointer where it exists, otherwise the newest COMPLETE run of that source
        # — the same derivation _migrate_v8_to_v9 uses to backfill the pointer. The fallback matters
        # because a database can legitimately have entries before anything has written source_roots
        # (a restored backup, a hand-assembled fixture); without it "current" would be empty and
        # every report would silently show nothing at all, which is a worse failure than showing
        # history.
        runs = sorted(
            int(row[0])
            for row in c.execute(
                """SELECT COALESCE(
                     (SELECT sr.latest_complete_scan_run_id FROM source_roots sr
                      WHERE sr.source_fingerprint=r.source_root_fingerprint
                        AND sr.latest_complete_scan_run_id IS NOT NULL),
                     MAX(r.id))
                   FROM scan_runs r WHERE r.status='COMPLETE'
                   GROUP BY r.source_root_fingerprint"""
            )
        )
        # No completed scan yet: the current inventory is empty, not "everything ever seen".
        where = (
            "{alias}scan_run_id IN (" + ",".join(str(run) for run in runs) + ")" if runs else "0"
        )
        # Drop dependants before the two base views they reference. The whole refresh is part of the
        # scanner's pointer transaction, so readers see either the previous complete layer or this
        # complete replacement — never a half-defined set of views.
        for view in (
            "current_preservation_assessments",
            "current_record_series_assignments",
            "current_collection_clusters",
            "current_collection_members",
            "current_content_overlap_results",
            "current_content_relationships",
            "current_relationships",
            "current_relationship_groups",
            "current_relationship_group_members",
            "current_exact_duplicate_groups",
            "current_exact_duplicate_members",
            "current_projects",
            "current_analysis_artifacts",
            "current_content_objects",
            "current_classifications",
            "current_entries",
        ):
            c.execute(f"DROP VIEW IF EXISTS {view}")
        c.execute(
            "CREATE VIEW current_entries AS SELECT * FROM filesystem_entries "
            "WHERE " + where.format(alias="")
        )
        c.execute(
            "CREATE VIEW current_classifications AS SELECT c.* FROM classifications c "
            "JOIN filesystem_entries e ON e.id=c.entry_id WHERE " + where.format(alias="e.")
        )
        # Content identity remains global in storage, but a *current* content object must be
        # reachable from at least one current path. Starting from current entries makes accumulated
        # scan history irrelevant to the work of materialising this set.
        c.execute(
            """CREATE VIEW current_content_objects AS
               SELECT co.* FROM content_objects co JOIN (
                 SELECT DISTINCT l.content_object_id FROM entry_content_links l
                 JOIN filesystem_entries e ON e.id=l.entry_id WHERE """
            + where.format(alias="e.")
            + ") live ON live.content_object_id=co.id"
        )
        c.execute(
            """CREATE VIEW current_analysis_artifacts AS
               SELECT a.* FROM analysis_artifacts a
               JOIN current_content_objects co ON co.id=a.content_object_id"""
        )
        c.execute(
            """CREATE VIEW current_projects AS
               SELECT p.* FROM projects p
               JOIN current_entries e ON e.id=p.root_entry_id"""
        )
        c.execute(
            """CREATE VIEW current_exact_duplicate_members AS
               SELECT m.* FROM exact_duplicate_members m
               JOIN current_entries e ON e.id=m.entry_id"""
        )
        # The stored group row is historical identity. Its current projection recomputes cardinality
        # from current members and ceases to be a group below two members. If an old canonical entry
        # is no longer current, use a deterministic current fallback until the analyser next writes
        # the group's fully evaluated canonical choice.
        c.execute(
            """CREATE VIEW current_exact_duplicate_groups AS
               SELECT g.id,g.content_object_id,g.full_hash,g.size_bytes,COUNT(*) member_count,
                      COUNT(DISTINCT CASE
                              WHEN e.device_id IS NOT NULL AND e.inode_or_file_id IS NOT NULL
                              THEN e.device_id || ':' || e.inode_or_file_id
                              ELSE 'entry:' || m.entry_id END) distinct_inode_count,
                      COALESCE(MAX(CASE WHEN m.entry_id=g.canonical_entry_id THEN m.entry_id END),
                               MIN(m.entry_id)) canonical_entry_id,
                      g.canonical_selection_reason,g.verified
               FROM exact_duplicate_groups g
               JOIN current_exact_duplicate_members m ON m.group_id=g.id
               JOIN current_entries e ON e.id=m.entry_id
               GROUP BY g.id,g.content_object_id,g.full_hash,g.size_bytes,
                        g.canonical_selection_reason,g.verified
               HAVING COUNT(*)>=2"""
        )
        c.execute(
            """CREATE VIEW current_relationship_group_members AS
               SELECT m.* FROM relationship_group_members m
               JOIN current_content_objects co ON co.id=m.content_object_id"""
        )
        c.execute(
            """CREATE VIEW current_relationship_groups AS
               SELECT g.* FROM relationship_groups g
               JOIN current_relationship_group_members m ON m.group_id=g.id
               GROUP BY g.id,g.group_type,g.group_key,g.relationship_version,g.evidence_json,
                        g.created_at,g.updated_at
               HAVING COUNT(*)>=2"""
        )

        def endpoint_is_current(type_sql: str, id_sql: str) -> str:
            return f"""(
                ({type_sql} IN ('ENTRY','DIRECTORY','ARCHIVE') AND
                 {id_sql} IN (SELECT id FROM current_entries))
                OR ({type_sql}='CONTENT_OBJECT' AND
                    {id_sql} IN (SELECT id FROM current_content_objects))
                OR ({type_sql}='PROJECT' AND
                    {id_sql} IN (SELECT id FROM current_projects))
                OR ({type_sql}='DUPLICATE_GROUP' AND
                    {id_sql} IN (SELECT id FROM current_exact_duplicate_groups))
            )"""

        # Legacy relationships use typed, polymorphic ids. Both endpoints must still be reachable
        # from the current inventory; an edge to one retired endpoint is historical evidence only.
        c.execute(
            "CREATE VIEW current_relationships AS SELECT r.* FROM relationships r WHERE "
            + endpoint_is_current("r.source_type", "r.source_id")
            + " AND "
            + endpoint_is_current("r.target_type", "r.target_id")
        )
        c.execute(
            "CREATE VIEW current_content_relationships AS SELECT r.* FROM content_relationships r WHERE "
            + endpoint_is_current("r.source_type", "r.source_id")
            + " AND "
            + endpoint_is_current("r.target_type", "r.target_id")
        )
        c.execute(
            """CREATE VIEW current_content_overlap_results AS
               SELECT r.* FROM content_overlap_results r
               JOIN current_content_objects a ON a.id=r.content_object_a_id
               JOIN current_content_objects b ON b.id=r.content_object_b_id"""
        )
        c.execute(
            """CREATE VIEW current_collection_members AS
               SELECT m.* FROM collection_members m WHERE
                 (m.member_type='ENTRY' AND m.member_id IN (SELECT id FROM current_entries))
                 OR (m.member_type='CONTENT_OBJECT' AND
                     m.member_id IN (SELECT id FROM current_content_objects))"""
        )
        c.execute(
            """CREATE VIEW current_collection_clusters AS
               SELECT c.* FROM collection_clusters c
               WHERE EXISTS (SELECT 1 FROM current_collection_members m WHERE m.cluster_id=c.id)"""
        )
        c.execute(
            """CREATE VIEW current_record_series_assignments AS
               SELECT a.* FROM record_series_assignments a WHERE
                 (a.target_type='ENTRY' AND a.target_id IN (SELECT id FROM current_entries))
                 OR (a.target_type='COLLECTION' AND
                     a.target_id IN (SELECT id FROM current_collection_clusters))"""
        )
        c.execute(
            """CREATE VIEW current_preservation_assessments AS
               SELECT a.* FROM preservation_assessments a WHERE
                 (a.target_type='ENTRY' AND a.target_id IN (SELECT id FROM current_entries))
                 OR (a.target_type='CONTENT_OBJECT' AND
                     a.target_id IN (SELECT id FROM current_content_objects))"""
        )
        return frozenset(runs)

    # Composite indexes on filesystem_entries, plus the drops they make possible. Created here
    # rather than in SCHEMA because parent_entry_id and source_root_id are legacy columns that
    # _ensure_legacy_columns may only just have added.
    _ENTRY_INDEXES = (
        # Child-by-parent was planned as "SEARCH ... USING idx_entries_run (scan_run_id=?)" — a
        # seek that still visits every row of the run. On a 1.3M-entry inventory that made one
        # quickstart stage a 58-hour operation.
        "CREATE INDEX IF NOT EXISTS idx_entries_run_parent_name ON filesystem_entries(scan_run_id,parent_entry_id,name)",
        # Rename detection looks up by (run, size); size alone matched across every historical scan.
        # NOT partial: the rename queries join file_signatures rather than stating entry_type='file',
        # so a partial predicate would make the planner ignore this index.
        "CREATE INDEX IF NOT EXISTS idx_entries_run_size ON filesystem_entries(scan_run_id,size_bytes)",
        # Reappearance-after-missing looks up a path within a source root.
        "CREATE INDEX IF NOT EXISTS idx_entries_source_path ON filesystem_entries(source_root_id,relative_path)",
        # The exact-duplicate size funnel only ever groups files, and directory rows never enter it —
        # so the bare-size index is partial on entry_type='file'. That skips the index write for every
        # directory entry (a real fraction of a deep tree) and drops those rows from the index
        # entirely. Every consumer states entry_type='file', which is what keeps the planner using it.
        "CREATE INDEX IF NOT EXISTS idx_entries_size_file ON filesystem_entries(size_bytes) WHERE entry_type='file'",
    )
    # Byte-for-byte duplicates of a UNIQUE constraint's automatic index, or a redundant prefix of
    # one of the composites above: pure write amplification and ~365 MB on a real inventory.
    # idx_entries_suffix is deliberately *not* here — it serves a dashboard query that filters on
    # that column alone (see tests/test_dashboard_indexes.py).
    _SUPERSEDED_INDEXES = (
        "idx_entries_run_relative",  # == UNIQUE(scan_run_id,relative_path)
        "idx_entries_run",  # prefix of idx_entries_run_parent_name and the UNIQUE index
        # 172 MB, and it was kept as recently as the last review because the dashboard search box
        # was the one query that planned through it. Scoping that search to current_entries made
        # UNIQUE(scan_run_id,relative_path) serve both the filter and the ORDER BY, so the plan and
        # the timing are now identical with and without it. Correctness bought the deletion.
        "idx_entries_path",
        "idx_content_hash",  # == UNIQUE(hash_algorithm,full_hash,size_bytes)
        "idx_artifact_lookup",  # == UNIQUE(content_object_id,analyser_name,analyser_version,configuration_fingerprint)
        "idx_entries_size",  # superseded by the partial idx_entries_size_file (files-only)
    )

    def _ensure_entry_indexes(self, c: sqlite3.Connection) -> None:
        for statement in self._ENTRY_INDEXES:
            c.execute(statement)
        for index in self._SUPERSEDED_INDEXES:
            c.execute(f"DROP INDEX IF EXISTS {index}")

    @staticmethod
    def _rename_analyser_columns(c: sqlite3.Connection) -> None:
        """Heal a database created before the British-spelling rename.

        ``analysis_artifacts.analyzer_name``/``analyzer_version`` became ``analyser_*``. A database
        created with the old spelling keeps the old columns (``CREATE TABLE IF NOT EXISTS`` never
        alters them), so every ``SELECT analyser_name`` fails with "no such column".

        Runs before ``SCHEMA`` in :meth:`initialize`. The four ``*_metadata`` compat views read
        these columns and are dropped first so ``RENAME COLUMN`` has no view referencing the column
        to rewrite (and cannot trip over one that names the not-yet-existing new column); the
        subsequent ``executescript(SCHEMA)`` recreates them with the British names. ``RENAME COLUMN``
        updates the dependent indexes automatically. Guarded by column existence, so it is a cheap
        no-op on a fresh or already-migrated database and safe to run on every open.
        """
        existing = {row[1] for row in c.execute("PRAGMA table_info(analysis_artifacts)")}
        renames = [
            (old, new)
            for old, new in (("analyzer_name", "analyser_name"), ("analyzer_version", "analyser_version"))
            if old in existing and new not in existing
        ]
        if not renames:
            return
        for view in ("document_metadata", "image_metadata", "media_metadata", "archive_metadata"):
            c.execute(f"DROP VIEW IF EXISTS {view}")
        for old, new in renames:
            c.execute(f"ALTER TABLE analysis_artifacts RENAME COLUMN {old} TO {new}")

    @staticmethod
    def _ensure_columns(c: sqlite3.Connection, table: str, columns: dict[str, str]) -> None:
        """Add only nullable/defaulted columns so interrupted upgrades are safe to repeat."""
        existing = {row[1] for row in c.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns.items():
            if name not in existing:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    def _ensure_legacy_columns(self, c: sqlite3.Connection) -> None:
        # CREATE TABLE IF NOT EXISTS cannot evolve a v1 table.  These additions cover every
        # column written by the scanner and are deliberately additive/non-destructive.
        self._ensure_columns(
            c,
            "scan_runs",
            {
                "completed_at": "TEXT",
                "hostname": "TEXT",
                "platform": "TEXT",
                "python_version": "TEXT",
                "config_hash": "TEXT",
                "files_seen": "INTEGER DEFAULT 0",
                "directories_seen": "INTEGER DEFAULT 0",
                "symlinks_seen": "INTEGER DEFAULT 0",
                "errors_seen": "INTEGER DEFAULT 0",
                "bytes_seen": "INTEGER DEFAULT 0",
                "last_checkpoint_at": "TEXT",
                "frontier_json": "TEXT",
            },
        )
        self._ensure_columns(
            c,
            "filesystem_entries",
            {
                "parent_entry_id": "INTEGER",
                "source_root_id": "INTEGER",
                "suffix": "TEXT",
                "is_hidden": "INTEGER DEFAULT 0",
                "is_symlink": "INTEGER DEFAULT 0",
                "symlink_target": "TEXT",
                "device_id": "INTEGER",
                "inode_or_file_id": "INTEGER",
                "nlink": "INTEGER",
                "mode": "INTEGER",
                "owner": "TEXT",
                "group_name": "TEXT",
                "created_at": "REAL",
                "modified_at": "REAL",
                "metadata_changed_at": "REAL",
                "accessed_at": "REAL",
                "birth_time_available": "INTEGER DEFAULT 0",
                "scan_status": "TEXT",
                "read_error": "TEXT",
                "first_seen_at": "TEXT",
                "last_seen_at": "TEXT",
            },
        )
        # "Which scan is the current inventory" is a stored fact, not a subquery — see
        # _migrate_v8_to_v9 and DriveScanner.
        self._ensure_columns(
            c, "source_roots", {"latest_complete_scan_run_id": "INTEGER REFERENCES scan_runs(id)"}
        )
        # A duplicate group's identity is the content it groups, not its insertion order — see
        # _migrate_v7_to_v8 and analysers/exact_duplicates.
        self._ensure_columns(
            c, "exact_duplicate_groups", {"content_object_id": "INTEGER REFERENCES content_objects(id)"}
        )
        self._ensure_columns(
            c,
            "file_signatures",
            {
                "extension_mime": "TEXT",
                "detected_mime": "TEXT",
                "detected_type": "TEXT",
                "signature_source": "TEXT",
                "quick_hash": "TEXT",
                "hash_error": "TEXT",
                "full_hash_computed_at": "TEXT",
            },
        )

    @staticmethod
    def _migrate_v1_to_v2(c: sqlite3.Connection) -> None:
        """Backfill verified legacy hashes without removing any v1 data."""
        c.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (2)")
        rows = c.execute("""SELECT e.id,e.scan_run_id,e.size_bytes,s.hash_algorithm,s.full_hash,
            s.hash_status FROM filesystem_entries e JOIN file_signatures s ON s.entry_id=e.id
            WHERE e.entry_type='file' AND s.full_hash IS NOT NULL AND s.hash_status IN ('VERIFIED','OK')""").fetchall()
        for row in rows:
            c.execute(
                """INSERT OR IGNORE INTO content_objects(hash_algorithm,full_hash,size_bytes,
                created_by_scan_run_id,verification_status) VALUES(?,?,?,?, 'VERIFIED')""",
                (row[3] or "sha256", row[4], row[2], row[1]),
            )
            obj = c.execute(
                "SELECT id FROM content_objects WHERE hash_algorithm=? AND full_hash=? AND size_bytes=?",
                (row[3] or "sha256", row[4], row[2]),
            ).fetchone()
            c.execute(
                """INSERT OR IGNORE INTO entry_content_links(entry_id,content_object_id,
                link_status,size_verified,hash_verified,entry_stat_fingerprint) VALUES(?,?, 'VERIFIED',1,1,'')""",
                (row[0], obj[0]),
            )

    @staticmethod
    def _migrate_v3_to_v4(c: sqlite3.Connection, batch_size: int = 1000) -> None:
        """Resumable, batched summary migration; safe after an interruption or restart."""
        progress = c.execute(
            "SELECT cursor_value,status FROM migration_progress WHERE migration_version=4"
        ).fetchone()
        cursor = int(progress[0]) if progress else 0
        c.execute(
            "INSERT OR IGNORE INTO migration_progress(migration_version,status) VALUES(4,'RUNNING')"
        )
        # The cursor makes this a real batched migration even on a very large existing DB.
        while True:
            rows = c.execute(
                "SELECT id FROM filesystem_entries WHERE id>? ORDER BY id LIMIT ?",
                (cursor, batch_size),
            ).fetchall()
            if not rows:
                break
            cursor = int(rows[-1][0])
            c.execute(
                "UPDATE migration_progress SET cursor_value=?,status='RUNNING',updated_at=CURRENT_TIMESTAMP WHERE migration_version=4",
                (cursor,),
            )
            c.commit()
        payload = json.dumps({"entries_cursor": cursor, "status": "complete"}, sort_keys=True)
        c.execute(
            "INSERT OR REPLACE INTO materialized_summaries(summary_key,value_json,source_scan_run_id) VALUES('migration_v4',?,NULL)",
            (payload,),
        )
        c.execute(
            "UPDATE migration_progress SET status='COMPLETED',updated_at=CURRENT_TIMESTAMP WHERE migration_version=4"
        )
        c.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (4)")

    @staticmethod
    def _migrate_v4_to_v5(c: sqlite3.Connection) -> None:
        """Additive: backfill role-based canonical assignments from the single canonical copy.

        Existing exact-duplicate groups and their canonical entries stay valid; each is given a
        CANONICAL_LOCATION role so richer roles can be added later without losing the original.
        """
        c.execute(
            """INSERT OR IGNORE INTO canonical_assignments(target_group_type,target_group_id,canonical_role,entry_id,content_object_id,source)
               SELECT 'EXACT_DUPLICATE_GROUP', g.id, 'CANONICAL_LOCATION', g.canonical_entry_id,
                      (SELECT content_object_id FROM entry_content_links WHERE entry_id=g.canonical_entry_id),
                      'migration'
               FROM exact_duplicate_groups g WHERE g.canonical_entry_id IS NOT NULL"""
        )
        c.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (5)")

    @staticmethod
    def _migrate_v6_to_v7(c: sqlite3.Connection) -> None:
        """Give ``scan_entry_changes.entry_id`` the foreign key it always should have had.

        Without it, a delete-and-reinsert of an entry left change rows pointing at an id that no
        longer existed, silently. SQLite cannot add a foreign key in place, so this is a full table
        rebuild — on a multi-GB inventory that is a multi-minute, multi-GB operation, which is why
        it is a numbered migration and not a schema tweak. Repeating it after an interruption is
        safe: the scratch table is dropped first and the original is only removed once the copy is
        complete. Rows orphaned by the old behaviour keep their evidence with a NULL ``entry_id``
        rather than being deleted.
        """
        referenced = {row[2] for row in c.execute("PRAGMA foreign_key_list(scan_entry_changes)")}
        if "filesystem_entries" not in referenced:
            c.execute("DROP TABLE IF EXISTS scan_entry_changes_rebuild")
            c.execute(
                """CREATE TABLE scan_entry_changes_rebuild(id INTEGER PRIMARY KEY,
                   scan_run_id INTEGER NOT NULL REFERENCES scan_runs(id) ON DELETE CASCADE,
                   entry_id INTEGER REFERENCES filesystem_entries(id) ON DELETE CASCADE,
                   relative_path TEXT NOT NULL, change_status TEXT NOT NULL,
                   evidence_json TEXT NOT NULL DEFAULT '{}')"""
            )
            c.execute(
                """INSERT INTO scan_entry_changes_rebuild(id,scan_run_id,entry_id,relative_path,change_status,evidence_json)
                   SELECT ch.id,ch.scan_run_id,
                     CASE WHEN EXISTS(SELECT 1 FROM filesystem_entries e WHERE e.id=ch.entry_id)
                          THEN ch.entry_id END,
                     ch.relative_path,ch.change_status,ch.evidence_json
                   FROM scan_entry_changes ch"""
            )
            c.execute("DROP TABLE scan_entry_changes")
            c.execute("ALTER TABLE scan_entry_changes_rebuild RENAME TO scan_entry_changes")
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_changes_run_status ON scan_entry_changes(scan_run_id,change_status)"
            )
            c.execute(
                "CREATE INDEX IF NOT EXISTS idx_changes_entry ON scan_entry_changes(entry_id,id)"
            )
        c.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (7)")

    @staticmethod
    def _ensure_duplicate_group_identity(c: sqlite3.Connection) -> None:
        """The unique index that turns ``content_object_id`` into a duplicate group's natural key.

        Partial, so pre-migration rows with a NULL content object do not all collide.
        """
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_dupe_group_content "
            "ON exact_duplicate_groups(content_object_id) WHERE content_object_id IS NOT NULL"
        )

    @staticmethod
    def _ensure_change_identity(c: sqlite3.Connection) -> None:
        """A UNIQUE(scan_run_id, entry_id) index so the change diff can be windowed idempotently.

        Windowing the change-classification and missing-detection INSERTs means a resumed run may
        re-execute a window; ``INSERT OR IGNORE`` behind this index makes that a no-op instead of a
        duplicate. Built once (skipped when it already exists, so the dedup scan is not paid every
        open), after removing any pre-existing duplicate rows that would block the unique index —
        the same shape as ``_ensure_duplicate_group_identity``. NULL entry ids are left alone (a
        UNIQUE index treats them as distinct), so a change row without an entry never collides.
        """
        if c.execute(
            "SELECT 1 FROM sqlite_master WHERE type='index' AND name='idx_changes_run_entry'"
        ).fetchone():
            return
        c.execute(
            """DELETE FROM scan_entry_changes WHERE entry_id IS NOT NULL AND id NOT IN (
                 SELECT MIN(id) FROM scan_entry_changes WHERE entry_id IS NOT NULL
                 GROUP BY scan_run_id, entry_id)"""
        )
        c.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_changes_run_entry "
            "ON scan_entry_changes(scan_run_id, entry_id)"
        )

    #: Indexes that exist only for the duration of migration v8. Both statements below need one,
    #: neither had one, and both were written against a toy database where that did not show.
    _V8_HELPER_INDEXES = (
        "CREATE INDEX IF NOT EXISTS tmp_migration_content_hash_size ON content_objects(full_hash,size_bytes)",
        "CREATE INDEX IF NOT EXISTS tmp_migration_dupe_group_content ON exact_duplicate_groups(content_object_id)",
    )
    #: A lookup by (full_hash, size_bytes), which is *not* a usable prefix of
    #: UNIQUE(hash_algorithm, full_hash, size_bytes).
    _V8_BACKFILL = """UPDATE exact_duplicate_groups SET content_object_id=(
                        SELECT MIN(co.id) FROM content_objects co
                        WHERE co.full_hash=exact_duplicate_groups.full_hash
                          AND co.size_bytes=exact_duplicate_groups.size_bytes)
                      WHERE content_object_id IS NULL"""
    #: Defensive: should never fire (one content object per hash+size), but a collision would make
    #: the unique index un-creatable and block the whole upgrade. This was
    #: ``id NOT IN (SELECT MIN(id) … GROUP BY …)``, which on 181,071 real groups did not complete
    #: in eleven minutes. Read as "another group already claimed this content object": one indexed
    #: probe per row.
    _V8_DEDUPE = """UPDATE exact_duplicate_groups SET content_object_id=NULL
                    WHERE content_object_id IS NOT NULL AND EXISTS(
                      SELECT 1 FROM exact_duplicate_groups other
                      WHERE other.content_object_id=exact_duplicate_groups.content_object_id
                        AND other.id<exact_duplicate_groups.id)"""

    @classmethod
    def _migrate_v7_to_v8(cls, c: sqlite3.Connection) -> None:
        """Backfill each duplicate group's content object so its id can stay stable.

        Before this, duplicate analysis deleted every group and reinserted it, which reallocated
        ids and — for any user who had recorded a canonical override — violated the
        ``canonical_overrides`` foreign key, failing at stage 2 of 20 on every subsequent run.

        Two throwaway indexes, built in about a second, take the whole migration from "did not
        finish in eleven minutes" to 1.3 s on the real inventory. Same lesson as Phase 1, applied
        to the upgrade path itself.
        """
        for statement in cls._V8_HELPER_INDEXES:
            c.execute(statement)
        c.execute(cls._V8_BACKFILL)
        c.execute(cls._V8_DEDUPE)
        c.execute("DROP INDEX IF EXISTS tmp_migration_content_hash_size")
        c.execute("DROP INDEX IF EXISTS tmp_migration_dupe_group_content")
        c.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (8)")

    @staticmethod
    def _migrate_v8_to_v9(c: sqlite3.Connection) -> None:
        """Backfill each source root's latest COMPLETE scan run.

        From here on the scanner maintains this in the same transaction that marks a run COMPLETE,
        so "the current inventory" is a stored fact a query can bind as a parameter rather than a
        subquery the planner has to evaluate against the whole entries table.
        """
        c.execute(
            """UPDATE source_roots SET latest_complete_scan_run_id=(
                 SELECT MAX(run.id) FROM scan_runs run
                 WHERE run.source_root_fingerprint=source_roots.source_fingerprint
                   AND run.status='COMPLETE')"""
        )
        c.execute("INSERT OR IGNORE INTO schema_migrations(version) VALUES (9)")

    #: Why a scan run may not be pruned. Each is a place something still points at it, and the value
    #: is the reason a human sees when the prune declines. Ordered most-specific-first so the message
    #: names the strongest reason.
    _PRUNE_HOLDS = (
        (
            "current inventory",
            "SELECT latest_complete_scan_run_id FROM source_roots WHERE latest_complete_scan_run_id IS NOT NULL",
        ),
        (
            "a review session's baseline",
            "SELECT base_scan_run_id FROM review_sessions WHERE base_scan_run_id IS NOT NULL",
        ),
        (
            "a recorded review decision",
            """SELECT e.scan_run_id FROM review_decisions d
               JOIN filesystem_entries e ON e.id=d.target_id AND d.target_type='ENTRY'""",
        ),
        (
            "a user canonical override",
            """SELECT e.scan_run_id FROM canonical_overrides o
               JOIN filesystem_entries e ON e.id=o.canonical_entry_id""",
        ),
        (
            "a materialized summary",
            "SELECT source_scan_run_id FROM materialized_summaries WHERE source_scan_run_id IS NOT NULL",
        ),
    )

    #: Key inside source_roots.device_metadata_json holding the last measured hashing throughput.
    OBSERVED_THROUGHPUT_KEY = "observed_hash_bytes_per_second"

    def observed_hash_throughput(self, source_fingerprint: str) -> float | None:
        """What hashing this source last actually achieved, in bytes per second.

        Stored on the source root rather than in configuration: it is an observation about a drive,
        not a preference, and it must not survive being written to a config file the operator then
        copies to a different machine.
        """
        row = self.fetch_one(
            "SELECT device_metadata_json FROM source_roots WHERE source_fingerprint=?",
            (source_fingerprint,),
        )
        if not row:
            return None
        try:
            value = json.loads(row["device_metadata_json"] or "{}").get(
                self.OBSERVED_THROUGHPUT_KEY
            )
        except (TypeError, ValueError):
            return None
        return float(value) if isinstance(value, (int, float)) and value > 0 else None

    def record_hash_throughput(self, source_fingerprint: str, bytes_per_second: float) -> None:
        """Merge a throughput observation into the source root's device metadata."""
        if bytes_per_second <= 0:
            return
        c = self.connect()
        row = c.execute(
            "SELECT device_metadata_json FROM source_roots WHERE source_fingerprint=?",
            (source_fingerprint,),
        ).fetchone()
        if not row:
            return
        try:
            metadata = json.loads(row["device_metadata_json"] or "{}")
        except (TypeError, ValueError):
            metadata = {}
        if not isinstance(metadata, dict):
            metadata = {}
        metadata[self.OBSERVED_THROUGHPUT_KEY] = round(float(bytes_per_second), 2)
        c.execute(
            "UPDATE source_roots SET device_metadata_json=? WHERE source_fingerprint=?",
            (json.dumps(metadata, sort_keys=True), source_fingerprint),
        )

    def snapshot_retention_plan(self, keep_per_source: int = 3) -> dict[str, object]:
        """Which superseded snapshots could be pruned, which are held, and by what.

        A snapshot is the drive as it was, and a superseded snapshot's verdict is the audit trail
        this tool exists to produce — so history is *retained by default* and this is the explicit,
        inspectable way to bound it. Nothing is deleted by computing a plan.

        ``keep_per_source`` is the number of most-recent COMPLETE runs kept per source root, on top
        of everything held for a reason below. Incomplete and interrupted runs are prunable once
        they fall outside that window: they are not a picture of anything.

        The holds matter more than the deletions. ``review_decisions.target_id`` is a bare integer,
        not a foreign key, so deleting an entry a human made a decision about would leave the
        decision pointing at nothing — losing exactly the evidence the tool promises to keep. This
        refuses instead.
        """
        c = self.connect()
        held: dict[int, str] = {}
        for reason, sql in self._PRUNE_HOLDS:
            for row in c.execute(sql):
                if row[0] is not None:
                    held.setdefault(int(row[0]), reason)

        keep_per_source = max(0, int(keep_per_source))
        recent: set[int] = set()
        for row in c.execute(
            "SELECT DISTINCT source_root_fingerprint FROM scan_runs WHERE status='COMPLETE'"
        ):
            recent.update(
                int(r[0])
                for r in c.execute(
                    "SELECT id FROM scan_runs WHERE source_root_fingerprint=? AND status='COMPLETE' "
                    "ORDER BY id DESC LIMIT ?",
                    (row[0], keep_per_source),
                )
            )
        for run in recent:
            held.setdefault(run, f"within the {keep_per_source} most recent complete scans")

        prunable = []
        for row in c.execute(
            """SELECT r.id,r.status,r.completed_at,
                      (SELECT COUNT(*) FROM filesystem_entries e WHERE e.scan_run_id=r.id) entries
               FROM scan_runs r ORDER BY r.id"""
        ):
            if int(row[0]) in held:
                continue
            prunable.append(
                {
                    "scan_run_id": int(row[0]),
                    "status": row[1],
                    "completed_at": row[2],
                    "entries": int(row[3]),
                }
            )
        return {
            "keep_per_source": keep_per_source,
            "prunable": prunable,
            "entries_prunable": sum(int(item["entries"]) for item in prunable),
            "held": [{"scan_run_id": run, "reason": reason} for run, reason in sorted(held.items())],
        }

    def prune_snapshots(self, keep_per_source: int = 3) -> dict[str, object]:
        """Execute :meth:`snapshot_retention_plan`. Returns the plan that was applied.

        Entries cascade to their signatures, content links, classifications and lifecycle rows;
        ``content_objects`` are deliberately **not** touched, because content identity is
        snapshot-independent by design — that is what makes a file recognisable across drives — and
        an artifact keyed on content stays valid however many snapshots referenced it.
        """
        plan = self.snapshot_retention_plan(keep_per_source)
        prunable = plan["prunable"]
        assert isinstance(prunable, list)  # narrows dict[str, object] for the type checker
        runs = [int(item["scan_run_id"]) for item in prunable]
        if not runs:
            return plan
        c = self.connect()
        placeholders = ",".join("?" for _ in runs)
        # Entries first: ON DELETE CASCADE fans out from here, and foreign keys are enabled on this
        # connection so a hold this method failed to spot raises rather than orphaning a row.
        c.execute(f"DELETE FROM filesystem_entries WHERE scan_run_id IN ({placeholders})", runs)
        c.execute(f"DELETE FROM scan_entry_changes WHERE scan_run_id IN ({placeholders})", runs)
        c.execute(f"DELETE FROM scan_runs WHERE id IN ({placeholders})", runs)
        self.refresh_current_inventory_views()
        c.commit()
        return plan

    # Migration bookkeeping survives a purge: the schema itself is untouched, so a database that
    # reported itself migrated still is.
    _PURGE_KEEP: ClassVar[tuple[str, ...]] = ("schema_migrations", "migration_progress")

    def purge_runs(self, keep_job_id: int | None = None) -> dict[str, int]:
        """Delete every recorded run and everything derived from one. Returns rows deleted per table.

        Rows rather than the file: the dashboard, its background runner and any CLI process may each
        hold an open connection, and unlinking the database under them leaves them writing to a
        deleted inode.

        ``keep_job_id`` spares one ``jobs`` row: the job that is running this purge. Without it a
        purge cannot be tracked at all, because it deletes the row recording it mid-flight.
        """
        c = self.connect()
        tables = [
            str(row[0])
            for row in c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
            if str(row[0]) not in self._PURGE_KEEP
        ]
        # One unordered DELETE per table with foreign keys *off*, rather than a dependency-ordered
        # cascade: the entire graph is going, so ordering carries no information — and an unqualified
        # DELETE on a table with no live foreign keys takes SQLite's truncate path instead of walking
        # every row to check constraints. On a large inventory that is the difference between holding
        # the single WAL write lock for minutes (past every other connection's busy_timeout, which is
        # how a concurrent dashboard refresh got "database is locked") and holding it for moments.
        c.commit()  # PRAGMA foreign_keys is a silent no-op inside a transaction
        c.execute("PRAGMA foreign_keys=OFF")
        deleted = {}
        try:
            for table in tables:
                if table == "jobs" and keep_job_id is not None:
                    # Row-wise instead of truncate for this one table: jobs is small (a row per
                    # stage), so keeping the purge's own row costs nothing measurable.
                    count = c.execute("DELETE FROM jobs WHERE id<>?", (keep_job_id,)).rowcount
                else:
                    count = c.execute(f"DELETE FROM {table}").rowcount
                if count > 0:
                    deleted[table] = count
            c.commit()
        finally:
            c.rollback()  # no-op after a successful commit; drops the partial purge otherwise
            c.execute("PRAGMA foreign_keys=ON")
        self.refresh_current_inventory_views()
        c.commit()
        return deleted

    def backup(self, output: Path) -> Path:
        """Create a consistent SQLite backup without modifying the source database."""
        output = Path(output)
        if output.exists():
            raise FileExistsError(f"refusing to overwrite database backup: {output}")
        output.parent.mkdir(parents=True, exist_ok=True)
        source = self.connect()
        target = sqlite3.connect(output)
        try:
            source.backup(target)
        finally:
            target.close()
        return output

    def integrity_check(self) -> str:
        row = self.fetch_one("PRAGMA integrity_check")
        return str(row[0]) if row else "unknown"

    # Pragmas tuned for read workloads over a multi-GB file: memory-map the first 256MB so hot
    # pages skip the syscall path, give the page cache 64MB, and keep sort/temp scratch in RAM.
    _READ_PRAGMAS = (
        "PRAGMA query_only=ON",
        "PRAGMA busy_timeout=5000",
        "PRAGMA mmap_size=268435456",
        "PRAGMA cache_size=-65536",
        "PRAGMA temp_store=MEMORY",
    )

    def _open_read_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            f"file:{self.path.resolve()}?mode=ro", uri=True, factory=counters.Connection
        )
        conn.row_factory = sqlite3.Row
        for pragma in self._READ_PRAGMAS:
            conn.execute(pragma)
        return conn

    def _read_conn(self) -> sqlite3.Connection:
        """The calling thread's pooled read-only connection, opened on first use."""
        conn = getattr(self._local, "read_conn", None)
        if conn is None:
            conn = self._open_read_connection()
            self._local.read_conn = conn
        return conn

    def reader(self) -> "_ReadDB":
        """A read-only view whose fetch_* route through the per-thread read pool."""
        return _ReadDB(self)

    @contextmanager
    def read_connection(self) -> Iterator[sqlite3.Connection]:
        """Independent, short-lived read-only connection for report/backup workloads."""
        conn = self._open_read_connection()
        try:
            yield conn
        finally:
            conn.close()

    def optimize_after_write(self, analyse: bool = False) -> None:
        """Settle the file after a large write job so later reads stay fast.

        ``PRAGMA optimize`` refreshes stale query-planner stats; a TRUNCATE checkpoint keeps a giant
        WAL left by a scan from slowing every subsequent reader. ``analyse=True`` runs a full
        ``ANALYZE`` — worth it once after the first scan so the planner has real statistics.

        Note: ``ANALYZE`` is a SQLite SQL keyword, not English prose — it must keep the ``z``.
        """
        c = self.connect()
        if analyse:
            c.execute("ANALYZE")
        c.execute("PRAGMA optimize")
        c.commit()
        self.checkpoint_wal("TRUNCATE")

    def wal_bytes(self) -> int:
        """Size of the write-ahead log file right now, in bytes (0 if none exists).

        A settled database has a small or empty WAL; a WAL that grows without bound over a long
        stage is the symptom that a reader's snapshot is pinning it (checkpoints cannot advance past
        the oldest reader). Cheap enough to sample at every stage boundary, and only sampled there.
        """
        try:
            return (self.path.parent / f"{self.path.name}-wal").stat().st_size
        except OSError:
            return 0

    def checkpoint_wal(self, mode: str = "PASSIVE") -> tuple[int, int, int]:
        if mode not in {"PASSIVE", "FULL", "RESTART", "TRUNCATE"}:
            raise ValueError("invalid WAL checkpoint mode")
        row = self.connect().execute(f"PRAGMA wal_checkpoint({mode})").fetchone()
        self.connect().commit()
        if not row:
            return (0, 0, 0)
        return (int(row[0]), int(row[1]), int(row[2]))

    def vacuum(self) -> None:
        """Vacuum only when the filesystem has enough free room for a temporary copy.

        Also the one moment an existing database can adopt the larger page size (a fresh database
        takes it at creation). The rebuild a VACUUM performs is what would rewrite every page at the
        new size — but SQLite silently ignores a ``page_size`` change while the database is in WAL
        mode, so the switch is a no-op there. To make it stick, drop to a rollback journal for the
        duration: set the page size under ``journal_mode=DELETE``, VACUUM, then restore WAL. When the
        page size already matches, this simply rebuilds and reclaims free pages as before.
        """
        if (
            self.path.exists()
            and os.statvfs(self.path.parent).f_bavail * os.statvfs(self.path.parent).f_frsize
            < self.path.stat().st_size * 2
        ):
            raise OSError("insufficient free disk space for VACUUM")
        conn = self.connect()
        conn.commit()  # journal_mode changes and VACUUM cannot run inside a transaction
        conn.execute("PRAGMA journal_mode=DELETE")
        conn.execute("PRAGMA page_size=8192")
        conn.execute("VACUUM")
        conn.execute("PRAGMA journal_mode=WAL")

    # The five overview charts, stored verbatim so the dashboard never re-runs these full-table
    # aggregates on a page load. Column order matches what DashboardService renders.
    #
    # All but scan_history describe the drive *now*. Artifacts are content-level and reusable across
    # snapshots, but a current completion count still includes only artifacts reachable from a
    # current path; otherwise deleted content accumulates forever in the dashboard metric.
    _CHART_QUERIES: ClassVar[dict[str, tuple[tuple[str, ...], str]]] = {
        "file_types": (
            ("suffix", "files", "bytes"),
            "SELECT COALESCE(suffix,'(none)') suffix,COUNT(*) files,SUM(size_bytes) bytes FROM current_entries WHERE entry_type='file' GROUP BY suffix ORDER BY bytes DESC LIMIT 20",
        ),
        "classification_bytes": (
            ("classification", "files", "bytes"),
            "SELECT COALESCE(c.classification,'UNCLASSIFIED') classification,COUNT(*) files,SUM(e.size_bytes) bytes FROM current_entries e LEFT JOIN classifications c ON c.entry_id=e.id WHERE e.entry_type='file' GROUP BY c.classification ORDER BY bytes DESC LIMIT 20",
        ),
        "top_level": (
            ("top_level", "files", "bytes"),
            "SELECT CASE WHEN instr(relative_path,'/')=0 THEN relative_path ELSE substr(relative_path,1,instr(relative_path,'/')-1) END top_level,COUNT(*) files,SUM(size_bytes) bytes FROM current_entries WHERE entry_type='file' GROUP BY top_level ORDER BY bytes DESC LIMIT 20",
        ),
        "scan_history": (
            ("id", "status", "files_seen", "bytes_seen", "completed_at"),
            "SELECT id,status,files_seen,bytes_seen,COALESCE(completed_at,'') completed_at FROM scan_runs ORDER BY id DESC LIMIT 20",
        ),
        "analyser_completion": (
            ("analyser_name", "status", "count"),
            "SELECT analyser_name,status,COUNT(*) count FROM current_analysis_artifacts GROUP BY analyser_name,status ORDER BY analyser_name,status",
        ),
    }

    def refresh_materialized_summaries(self, scan_run_id: int | None = None) -> dict[str, int]:
        c = self.connect()

        def scalar(sql: str) -> int:
            return int(c.execute(sql).fetchone()[0])

        charts = {
            key: {
                "columns": list(columns),
                "rows": [
                    [str(row[column]) if row[column] is not None else "" for column in columns]
                    for row in c.execute(sql).fetchall()
                ],
            }
            for key, (columns, sql) in self._CHART_QUERIES.items()
        }
        values = {
            "overview": {
                "logical_bytes": scalar(
                    "SELECT COALESCE(SUM(size_bytes),0) FROM current_entries WHERE entry_type='file'"
                ),
                "unique_content_bytes": scalar(
                    "SELECT COALESCE(SUM(size_bytes),0) FROM current_content_objects"
                ),
                # Hard-link-honest: within a duplicate group, paths sharing one inode free nothing
                # when deleted, so reclaimable counts distinct inodes beyond the keeper, not paths.
                # A snapshot drive where every "copy" is a hard link reads as 0 reclaimable, rightly.
                "reclaimable_bytes": scalar(
                    "SELECT COALESCE(SUM(size_bytes*(distinct_inode_count-1)),0) "
                    "FROM current_exact_duplicate_groups"
                ),
                "entries": scalar("SELECT COUNT(*) FROM current_entries"),
                "content_objects": scalar("SELECT COUNT(*) FROM current_content_objects"),
                "analysis_artifacts": scalar("SELECT COUNT(*) FROM current_analysis_artifacts"),
                "sources": scalar("SELECT COUNT(*) FROM source_roots"),
                "duplicate_groups": scalar("SELECT COUNT(*) FROM current_exact_duplicate_groups"),
            },
            "charts": charts,
            "classifications": {
                str(r[0]): int(r[1])
                for r in c.execute(
                    "SELECT classification,COUNT(*) FROM current_classifications GROUP BY classification"
                )
            },
            "sources": {
                str(r[0]): {"files": int(r[1]), "bytes": int(r[2] or 0)}
                for r in c.execute(
                    "SELECT source_root,COUNT(*),COALESCE(SUM(size_bytes),0) FROM current_entries WHERE entry_type='file' GROUP BY source_root"
                )
            },
            "content_kinds": {
                str(r[0] or "UNKNOWN"): int(r[1])
                for r in c.execute(
                    "SELECT content_kind,COUNT(*) FROM current_content_objects GROUP BY content_kind"
                )
            },
            "review_sessions": {
                str(r[0]): int(r[1])
                for r in c.execute("SELECT status,COUNT(*) FROM review_sessions GROUP BY status")
            },
        }
        # Rolled back on failure. This upsert contends
        # with whatever the background worker is writing, so it is the statement most likely in the
        # whole codebase to raise SQLITE_BUSY. Python has already emitted its implicit BEGIN by then,
        # and an exception escaping here left this connection — which every dashboard request thread
        # shares — inside an open transaction for the life of the process. Its subsequent reads then
        # pinned a WAL snapshot instead of running in autocommit, so the *next* refresh failed as a
        # stale-snapshot upgrade (SQLITE_BUSY_SNAPSHOT): same "database is locked" text, raised
        # instantly, with busy_timeout never consulted. One timeout became every refresh failing
        # until the process restarted.
        with self.transaction() as writer:
            for key, value in values.items():
                writer.execute(
                    "INSERT INTO materialized_summaries(summary_key,value_json,source_scan_run_id,refreshed_at) VALUES(?,?,?,CURRENT_TIMESTAMP) ON CONFLICT(summary_key) DO UPDATE SET value_json=excluded.value_json,source_scan_run_id=excluded.source_scan_run_id,refreshed_at=CURRENT_TIMESTAMP",
                    (key, json.dumps(value, sort_keys=True), scan_run_id),
                )
        return {key: len(value) if isinstance(value, dict) else 0 for key, value in values.items()}

    def database_stats(self, check_integrity: bool = True) -> dict[str, int | str]:
        """Cheap COUNT-based stats, plus a full ``PRAGMA integrity_check`` unless disabled.

        The integrity check is O(database size) — fine for a deliberate CLI invocation, but far
        too slow to run on every dashboard page load on a multi-GB database (measured 50s+ on a
        1.9GB/1.5M-entry inventory). Callers on a hot path should pass ``check_integrity=False``.
        """
        c = self.connect()
        return {
            "schema_version": int(
                c.execute("SELECT COALESCE(MAX(version),0) FROM schema_migrations").fetchone()[0]
            ),
            "database_bytes": self.path.stat().st_size if self.path.exists() else 0,
            "entries": int(c.execute("SELECT COUNT(*) FROM filesystem_entries").fetchone()[0]),
            "content_objects": int(c.execute("SELECT COUNT(*) FROM content_objects").fetchone()[0]),
            "analysis_artifacts": int(
                c.execute("SELECT COUNT(*) FROM analysis_artifacts").fetchone()[0]
            ),
            "integrity": self.integrity_check() if check_integrity else "not checked",
        }

    # The write primitives below do NOT commit. A commit per row turned a bulk stage into one
    # fsync-bounded transaction per entry; the transaction belongs to the stage (or the batch),
    # which is the only level that knows what a consistent unit of work is.
    def get_or_create_content_object(
        self, algorithm: str, digest: str, size: int, scan_id: int | None = None
    ) -> int:
        cur = self.connect()
        cur.execute(
            """INSERT OR IGNORE INTO content_objects(hash_algorithm,full_hash,size_bytes,created_by_scan_run_id)
            VALUES(?,?,?,?)""",
            (algorithm, digest, size, scan_id),
        )
        row = cur.execute(
            "SELECT id FROM content_objects WHERE hash_algorithm=? AND full_hash=? AND size_bytes=?",
            (algorithm, digest, size),
        ).fetchone()
        return int(row[0])

    def link_entry_content(
        self, entry_id: int, content_object_id: int, stat_fingerprint: str, status: str = "VERIFIED"
    ) -> None:
        self.connect().execute(
            """INSERT INTO entry_content_links(entry_id,content_object_id,link_status,size_verified,hash_verified,entry_stat_fingerprint)
            VALUES(?,?,?,1,1,?) ON CONFLICT(entry_id) DO UPDATE SET content_object_id=excluded.content_object_id,link_status=excluded.link_status,
            size_verified=excluded.size_verified,hash_verified=excluded.hash_verified,entry_stat_fingerprint=excluded.entry_stat_fingerprint,linked_at=CURRENT_TIMESTAMP""",
            (entry_id, content_object_id, status, stat_fingerprint),
        )

    def is_analysis_current(
        self,
        content_object_id: int,
        analyser_name: str,
        analyser_version: str,
        config_fingerprint: str,
    ) -> bool:
        current = (
            self.fetch_one(
                """SELECT 1 FROM analysis_artifacts WHERE content_object_id=? AND analyser_name=? AND analyser_version=?
            AND configuration_fingerprint=? AND status='COMPLETED'""",
                (content_object_id, analyser_name, analyser_version, config_fingerprint),
            )
            is not None
        )
        counters.count("artifact_cache_hits" if current else "artifact_cache_misses")
        return current

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

    def execute_windowed(
        self,
        template: str,
        params: tuple,
        *,
        key: str,
        bounds: tuple[int | None, int | None],
        chunk: int = 250_000,
        on_window=None,
    ) -> None:
        """Run one set-based statement in id-windows, committing after each.

        ``template`` must contain the literal ``{window}`` where an ``AND {key} BETWEEN ? AND ?``
        clause is spliced; ``params`` are the statement's own parameters, in order, with the two
        window bounds appended per window. This is how the scan epilogue's O(entries) statements —
        parent linking, change classification, signature copy-forward, missing detection — become a
        sequence of bounded, committed, cancellable steps instead of one multi-hour transaction that
        pins the WAL, defers Ctrl-C, and rolls back hours of work if it fails near the end. The
        windowed statements are idempotent (a real upsert, or ``INSERT OR IGNORE`` behind a unique
        index), so re-running a window on resume changes nothing.

        ``on_window`` is invoked after each window commits — the scanner passes its cancellation
        check, so a stop lands within one window rather than after the whole statement.
        """
        lo, hi = bounds
        if lo is None or hi is None:
            return
        sql = template.format(window=f" AND {key} BETWEEN ? AND ?")
        conn = self.connect()
        start, hi = int(lo), int(hi)
        while start <= hi:
            end = min(start + max(1, chunk) - 1, hi)
            conn.execute(sql, (*params, start, end))
            conn.commit()
            if on_window is not None:
                on_window()
            start = end + 1

    def iter_keyset(self, sql, params, *, key_exprs, key_of, batch_size: int = 5_000):
        """Stream ``sql`` in keyset pages on the writer connection. See :func:`_keyset_pages`."""
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        yield from _keyset_pages(self.connect().execute, sql, params, key_exprs, key_of, batch_size)

    def execute(self, sql: str, params: tuple | dict = ()) -> sqlite3.Cursor:
        return self.connect().execute(sql, params)

    def executemany(self, sql: str, rows: list[tuple]) -> None:
        self.connect().executemany(sql, rows)
        self.connect().commit()

    def fetch_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self.execute(sql, params).fetchone()

    def fetch_all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self.execute(sql, params).fetchall()

    def iter_rows(
        self, sql: str, params: tuple | dict = (), batch_size: int = 1_000
    ) -> Iterator[sqlite3.Row]:
        """Stream bounded batches for inventory-scale maintenance and analysis work."""
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        cursor = self.execute(sql, params)
        while batch := cursor.fetchmany(batch_size):
            yield from batch

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
        assert cur.lastrowid is not None
        return cur.lastrowid

    # Columns an upsert must never overwrite: the conflict target itself, and the first/last-seen
    # bookkeeping that only makes sense as "when did we first observe this row".
    _ENTRY_UPSERT_KEEP = frozenset({"scan_run_id", "relative_path", "first_seen_at", "last_seen_at"})

    def insert_entry(self, values: dict[str, Any]) -> int:
        """Insert one entry, or refresh the existing one **in place**, returning its id.

        This used to be ``INSERT OR REPLACE``, which on conflict *deletes* the existing row: its
        ``file_signatures``, ``entry_content_links`` and ``classifications`` cascade away and the
        id is reallocated. Resuming an interrupted scan therefore destroyed verified hashes and
        erased ``PROTECTED`` markers — resume was a pessimisation and a safety hole. A real upsert
        preserves the id, so everything hanging off it survives.
        """
        keys = ",".join(values)
        marks = ",".join("?" for _ in values)
        updates = [f"{key}=excluded.{key}" for key in values if key not in self._ENTRY_UPSERT_KEEP]
        updates.append("last_seen_at=CURRENT_TIMESTAMP")
        row = self.connect().execute(
            f"INSERT INTO filesystem_entries({keys}) VALUES({marks}) "
            f"ON CONFLICT(scan_run_id,relative_path) DO UPDATE SET {','.join(updates)} RETURNING id",
            tuple(values.values()),
        ).fetchone()
        assert row is not None
        return int(row[0])


class _ReadDB:
    """Read-only view over a :class:`Database` exposing the fetch API the dashboard uses.

    It mirrors ``Database.fetch_one``/``fetch_all``/``iter_rows`` so read-only consumers (the
    dashboard service and GET endpoints) need no code change — they just receive this instead of
    the writer. Every query runs on the caller thread's pooled read-only connection.
    """

    def __init__(self, database: Database) -> None:
        self._db = database

    def fetch_one(self, sql: str, params: tuple | dict = ()) -> sqlite3.Row | None:
        return self._db._read_conn().execute(sql, params).fetchone()

    def fetch_all(self, sql: str, params: tuple | dict = ()) -> list[sqlite3.Row]:
        return self._db._read_conn().execute(sql, params).fetchall()

    def iter_rows(
        self, sql: str, params: tuple | dict = (), batch_size: int = 1_000
    ) -> Iterator[sqlite3.Row]:
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        cursor = self._db._read_conn().execute(sql, params)
        while batch := cursor.fetchmany(batch_size):
            yield from batch

    def iter_keyset(self, sql, params, *, key_exprs, key_of, batch_size: int = 5_000):
        """Stream ``sql`` in keyset pages on the thread's read connection. See :func:`_keyset_pages`.

        The read connection is reused, but each page is a fresh statement, so no snapshot is held
        across pages — which is the whole point: a long identity or artifact stage no longer pins the
        WAL while the writer commits behind it.
        """
        if batch_size < 1:
            raise ValueError("batch_size must be positive")
        yield from _keyset_pages(
            self._db._read_conn().execute, sql, params, key_exprs, key_of, batch_size
        )
