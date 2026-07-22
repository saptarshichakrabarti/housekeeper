"""Spec-named compatibility views over the normalized content-addressed store.

The creation prompt names per-domain tables (``document_metadata``, ``image_metadata``,
``directory_overlap_results`` …) that a downstream reader may ``SELECT`` from directly. The
normalized schema stores that information in ``analysis_artifacts`` / ``relationships`` /
``relationship_groups`` instead, so these read-only SQL views re-expose it under the spec's
names. These tests assert the views exist, are genuinely views (never shadow a base table), and
return the expected rows once analysis has run.
"""

from housekeeper.analysers.directory_overlap import run_directory_overlap_analysis
from housekeeper.analysers.document_versions import run_document_version_analysis
from housekeeper.analysers.documents import run_document_analysis
from housekeeper.analysers.exact_duplicates import run_exact_duplicate_analysis
from housekeeper.scanner import DriveScanner

COMPAT_VIEWS = [
    "document_metadata",
    "image_metadata",
    "media_metadata",
    "archive_metadata",
    "directory_overlap_results",
    "document_version_groups",
    "document_version_members",
    "image_similarity_groups",
    "image_similarity_members",
]


def test_all_compat_views_registered_as_views(database):
    rows = database.fetch_all(
        "SELECT name,type FROM sqlite_master WHERE name IN (%s)"
        % ",".join("?" * len(COMPAT_VIEWS)),
        tuple(COMPAT_VIEWS),
    )
    registered = {row["name"]: row["type"] for row in rows}
    assert set(registered) == set(COMPAT_VIEWS)
    # Every compatibility name must be a VIEW, never a base table shadowing normalized storage.
    assert all(kind == "view" for kind in registered.values())


def test_compat_views_queryable_when_empty(database):
    # A fresh database returns zero rows, not an error, from every compatibility view.
    for view in COMPAT_VIEWS:
        assert database.fetch_all(f"SELECT * FROM {view}") == []


def test_document_metadata_view_reflects_analysis(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.txt").write_text("the quick brown fox jumps over the lazy dog", encoding="utf-8")
    (root / "b.md").write_text("# heading\nmarkdown body content", encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    run_document_analysis(database, config)

    rows = database.fetch_all(
        "SELECT document_kind,character_count,word_count,extraction_status,normalized_text_hash "
        "FROM document_metadata ORDER BY character_count"
    )
    assert len(rows) == 2
    for row in rows:
        assert row["document_kind"] == "text"
        assert row["character_count"] > 0
        assert row["word_count"] > 0
        assert row["extraction_status"] == "COMPLETED"
        assert len(row["normalized_text_hash"]) == 64  # sha256 hex


def test_directory_overlap_results_view_reflects_analysis(config, database, tmp_path):
    root = tmp_path / "src"
    original = root / "Original"
    backup = root / "Backup"
    original.mkdir(parents=True)
    backup.mkdir(parents=True)
    for name in ("one.txt", "two.txt", "three.txt"):
        (original / name).write_text(f"payload for {name}", encoding="utf-8")
        (backup / name).write_text(f"payload for {name}", encoding="utf-8")
    config.section("directory_overlap")["minimum_files"] = 1
    config.section("directory_overlap")["minimum_bytes"] = 0
    DriveScanner(database, config).scan(root, incremental=False)
    run_exact_duplicate_analysis(database, config)  # hash files so directory hash-sets exist
    run_directory_overlap_analysis(database, config)

    rows = database.fetch_all(
        "SELECT directory_a_id,directory_b_id,shared_file_hashes,containment_a_in_b "
        "FROM directory_overlap_results"
    )
    assert len(rows) >= 1
    overlap = rows[0]
    assert overlap["directory_a_id"] != overlap["directory_b_id"]
    assert overlap["shared_file_hashes"] == 3
    assert overlap["containment_a_in_b"] == 1.0


def test_document_version_group_views_reflect_analysis(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    # Same base name with version suffixes and distinct content => a DOCUMENT_FAMILY group.
    (root / "report_v1.txt").write_text("first draft of the report body", encoding="utf-8")
    (root / "report_v2.txt").write_text("second draft with revised content", encoding="utf-8")
    (root / "report_final.txt").write_text("final approved version of report", encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    run_document_analysis(database, config)  # ensures content objects + links for every document
    run_document_version_analysis(database, config)

    groups = database.fetch_all(
        "SELECT id,normalized_family_name,review_required FROM document_version_groups"
    )
    assert len(groups) >= 1
    group = groups[0]
    assert group["review_required"] == 1
    members = database.fetch_all(
        "SELECT group_id,entry_id FROM document_version_members WHERE group_id=?",
        (group["id"],),
    )
    assert len(members) >= 2  # a version family has more than one member
