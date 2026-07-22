"""Review-manifest export, parsing, and validation."""

import pytest

from housekeeper.analyzers.exact_duplicates import run_exact_duplicate_analysis
from housekeeper.manifests import (
    export_review_manifest,
    load_manifest,
    validate_manifest_against_database,
    validate_manifest_schema,
)
from housekeeper.models import ManifestEntry
from housekeeper.policies import classify_all_entries
from housekeeper.scanner import DriveScanner


def _dup_db(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.bin").write_bytes(b"duplicate-bytes")
    (root / "b.bin").write_bytes(b"duplicate-bytes")
    DriveScanner(database, config).scan(root, incremental=False)
    run_exact_duplicate_analysis(database, config)
    classify_all_entries(database, config)
    return root


def test_export_review_manifest_writes_header_and_rows(config, database, tmp_path):
    _dup_db(config, database, tmp_path)
    out = tmp_path / "manifest.csv"
    export_review_manifest(database, out, {"REVIEW_SAFE"})
    entries = load_manifest(out)
    assert entries  # the non-canonical duplicate is present
    assert all(entry.classification == "REVIEW_SAFE" for entry in entries)
    assert all(entry.approved is False for entry in entries)  # nothing approved by default


def test_export_refuses_to_overwrite(config, database, tmp_path):
    _dup_db(config, database, tmp_path)
    out = tmp_path / "manifest.csv"
    export_review_manifest(database, out, {"REVIEW_SAFE"})
    with pytest.raises(FileExistsError):
        export_review_manifest(database, out, {"REVIEW_SAFE"})


def test_load_jsonl_manifest(tmp_path):
    path = tmp_path / "m.jsonl"
    path.write_text(
        '{"approved": true, "entry_id": 1, "source_path": "/x/a", "relative_path": "a",'
        ' "size_bytes": 3, "expected_sha256": "abc", "classification": "REVIEW_SAFE",'
        ' "confidence": 1.0, "reason_codes": [], "explanation": ""}\n',
        encoding="utf-8",
    )
    entries = load_manifest(path)
    assert entries[0].approved is True
    assert entries[0].entry_id == 1


def test_schema_detects_duplicate_rows_and_missing_hash():
    entries = [
        ManifestEntry(True, 1, "/x/a", "a", 3, "", "REVIEW_SAFE", 1.0, [], ""),
        ManifestEntry(True, 1, "/x/a", "a", 3, "hash", "REVIEW_SAFE", 1.0, [], ""),
    ]
    errors = validate_manifest_schema(entries)
    assert any("duplicate" in e for e in errors)
    assert any("invalid approved" in e for e in errors)


def test_database_validation_flags_source_drift(config, database, tmp_path):
    _dup_db(config, database, tmp_path)
    row = database.fetch_one(
        "SELECT e.id,e.relative_path,e.size_bytes,s.full_hash FROM filesystem_entries e JOIN file_signatures s ON s.entry_id=e.id WHERE e.entry_type='file' LIMIT 1"
    )
    drifted = ManifestEntry(
        True, row["id"], "/wrong/path", row["relative_path"], row["size_bytes"], row["full_hash"],
        "REVIEW_SAFE", 1.0, [], "",
    )
    errors = validate_manifest_against_database([drifted], database)
    assert any("drift" in e for e in errors)


def test_database_validation_flags_hash_mismatch(config, database, tmp_path):
    _dup_db(config, database, tmp_path)
    row = database.fetch_one(
        "SELECT e.id,e.absolute_path,e.relative_path,e.size_bytes FROM filesystem_entries e WHERE e.entry_type='file' LIMIT 1"
    )
    bad = ManifestEntry(
        True, row["id"], row["absolute_path"], row["relative_path"], row["size_bytes"],
        "0" * 64, "REVIEW_SAFE", 1.0, [], "",
    )
    errors = validate_manifest_against_database([bad], database)
    assert any("unverified" in e for e in errors)
