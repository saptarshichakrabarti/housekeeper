"""Current-state *output* must never contain a historical snapshot's rows.

[[test_current_inventory]] covers the analysers. This file covers the current-state relational layer
and its downstream reports, exports, manifests, dashboard, and content-analysis work plan. Repeated
snapshots and deleted entities are different adversarial shapes: the former catches multiplication;
the latter catches historical derived rows still presented as current facts.

The fix is structural: current-state consumers read the ``current_*`` views instead of historical
base tables, so scoping is a property of the relation you name rather than a parameter you have to
remember. These tests pin the behaviour, not the mechanism.
"""

from pathlib import Path

import pytest

from housekeeper.analysers.registry import _store_text, run_content_analysis
from housekeeper.core import counters
from housekeeper.manifests import export_review_manifest
from housekeeper.policies import classify_all_entries
from housekeeper.reports.contexts import (
    build_document_versions_context,
    build_duplicates_context,
    build_errors_context,
    build_image_groups_context,
    build_inventory_context,
    build_projects_context,
    build_summary_context,
)
from housekeeper.reports.exports import export_csv
from housekeeper.scanner import DriveScanner

FILES = 6


@pytest.fixture
def twice_scanned(config, database, tmp_path):
    """One unchanged tree, scanned twice. Two snapshots, one current inventory."""
    root = tmp_path / "drive"
    root.mkdir()
    for i in range(FILES):
        (root / f"note-{i}.txt").write_text(f"contents of note {i}\n")
    scanner = DriveScanner(database, config)
    scanner.scan(root)
    scanner.scan(root)
    assert database.fetch_one("SELECT COUNT(*) n FROM scan_runs")["n"] == 2
    assert (
        database.fetch_one("SELECT COUNT(*) n FROM filesystem_entries WHERE entry_type='file'")["n"]
        == FILES * 2
    ), "both snapshots must really be stored — otherwise these tests prove nothing"
    classify_all_entries(database, config)
    current = database.fetch_one("SELECT MAX(id) id FROM scan_runs WHERE status='COMPLETE'")["id"]
    return database, config, root, current


def test_summary_report_counts_the_drive_not_the_history(twice_scanned):
    database, config, _root, _run = twice_scanned
    summary = build_summary_context(database, config)
    assert summary["file_count"] == FILES
    assert sum(summary["classifications"].values()) == FILES


def test_inventory_report_does_not_double_its_directories(twice_scanned):
    database, config, _root, _run = twice_scanned
    inventory = build_inventory_context(database, config)
    assert sum(row["n"] for row in inventory["directories"]) == FILES
    assert sum(row["n"] for row in inventory["extensions"]) == FILES


def test_materialized_overview_counts_the_current_inventory(twice_scanned):
    database, _config, _root, run = twice_scanned
    database.refresh_materialized_summaries(run)
    overview, _ = _summary(database, "overview")
    assert overview["entries"] == database.fetch_one(
        "SELECT COUNT(*) n FROM current_entries"
    )["n"]
    charts, _ = _summary(database, "charts")
    file_types = {row[0]: int(row[1]) for row in charts["file_types"]["rows"]}
    assert file_types.get(".txt") == FILES
    # scan_history is the one chart that is *supposed* to span snapshots.
    assert len(charts["scan_history"]["rows"]) == 2


def _summary(database, key):
    import json

    row = database.fetch_one(
        "SELECT value_json,refreshed_at FROM materialized_summaries WHERE summary_key=?", (key,)
    )
    return json.loads(row["value_json"]), row["refreshed_at"]


def test_recommendation_export_and_manifest_do_not_duplicate_rows(twice_scanned, tmp_path):
    database, _config, _root, _run = twice_scanned
    database.connect().execute(
        "UPDATE classifications SET classification='REVIEW_SAFE' WHERE entry_id IN "
        "(SELECT id FROM current_entries WHERE entry_type='file')"
    )
    database.connect().commit()

    csv_path = export_csv(database, tmp_path / "out" / "recommendations.csv")
    assert len(csv_path.read_text().strip().splitlines()) == FILES + 1  # + header

    manifest = export_review_manifest(
        database, tmp_path / "out" / "review.csv", {"REVIEW_SAFE"}
    )
    assert len(manifest.read_text().strip().splitlines()) == FILES + 1


def test_dashboard_review_queue_shows_each_file_once(twice_scanned):
    pytest.importorskip("fastapi")
    from housekeeper.dashboard.filters import ReviewFilter
    from housekeeper.dashboard.services import DashboardService

    database, _config, _root, _run = twice_scanned
    rows = DashboardService(database).review_rows(ReviewFilter(), limit=100, after_id=0)
    assert len(rows) == FILES
    assert len({row.relative_path for row in rows}) == FILES


def test_content_analysis_after_a_rescan_still_does_the_work_it_owes(config, database, tmp_path):
    """The silent-skip defect: an analyser that reports success having analysed nothing.

    The work plan grouped by content object while selecting non-aggregated entry columns, so SQLite
    returned an arbitrary snapshot's row; the caller then discarded any object whose row came from a
    run other than the requested one. After a rescan that is *most* of them, and the stage reported
    zero pending work rather than doing it.
    """
    root = tmp_path / "drive"
    root.mkdir()
    for i in range(FILES):
        (root / f"doc-{i}.txt").write_text(f"body {i}\n")
    scanner = DriveScanner(database, config)
    scanner.scan(root)
    scanner.scan(root)

    def artifacts() -> int:
        return database.fetch_one(
            "SELECT COUNT(*) n FROM analysis_artifacts WHERE analyser_name='documents'"
        )["n"]

    first = run_content_analysis(database, config, "documents")
    assert first["completed"] == FILES, first
    assert artifacts() == FILES

    # A configuration change the documents analyser can actually see invalidates its artifacts,
    # so the rerun owes exactly the same work again.
    config.section("documents")["max_text_characters"] = 4321
    second = run_content_analysis(database, config, "documents")
    assert second["completed"] == FILES, (
        f"analyser skipped work it owed after a rescan + config change: {second}"
    )


def test_content_analysis_ignores_a_file_that_only_exists_in_history(config, database, tmp_path):
    """The other direction: a deleted file must not keep generating work forever."""
    root = tmp_path / "drive"
    root.mkdir()
    (root / "keep.txt").write_text("still here\n")
    (root / "gone.txt").write_text("deleted before the second scan\n")
    scanner = DriveScanner(database, config)
    scanner.scan(root)
    (root / "gone.txt").unlink()
    scanner.scan(root)

    counts = run_content_analysis(database, config, "documents")
    assert counts["completed"] == 1, counts
    analysed = {
        Path(row["absolute_path"]).name
        for row in database.fetch_all(
            """SELECT e.absolute_path FROM analysis_artifacts a
               JOIN entry_content_links l ON l.content_object_id=a.content_object_id
               JOIN filesystem_entries e ON e.id=l.entry_id
               WHERE a.analyser_name='documents'"""
        )
    }
    assert analysed == {"keep.txt"}


def test_storing_document_text_does_not_commit_per_blob(database, config):
    """Definition-of-done #5 says no per-object commits; _store_text was three for three."""
    database.connect().execute(
        "INSERT INTO content_objects(id,hash_algorithm,full_hash,size_bytes) VALUES"
        "(1,'sha256','a',1),(2,'sha256','b',2),(3,'sha256','c',3)"
    )
    database.connect().commit()
    with counters.recording() as counted:
        for content_id in (1, 2, 3):
            _store_text(database, content_id, f"text for {content_id}", config)
    assert counted["commits"] == 0, (
        f"_store_text committed {counted['commits']} times; the enclosing batch owns the "
        "transaction boundary"
    )
    database.connect().commit()
    assert database.fetch_one("SELECT COUNT(*) n FROM content_text_blobs")["n"] == 3


def test_deleting_every_current_member_retires_all_current_derived_output(
    config, database, tmp_path
):
    """Historical evidence stays stored, but no current report/API may present it as live.

    An unchanged second scan only proves rows do not double. This shape creates duplicate, project,
    artifact, relationship, overlap, collection, record-series and preservation rows, deletes every
    path that made them current, then proves the base audit trail survives while every current
    projection becomes empty.
    """
    from housekeeper.analysers.exact_duplicates import run_exact_duplicate_analysis
    from housekeeper.analysers.projects import run_project_analysis
    from housekeeper.relationships import replace_relationship_group, upsert_content_relationship

    root = tmp_path / "retirement-drive"
    project = root / "retired-project"
    project.mkdir(parents=True)
    (project / "pyproject.toml").write_text("[project]\nname='retired-project'\n")
    (project / "duplicate-a.txt").write_text("same duplicate payload\n")
    (project / "duplicate-b.txt").write_text("same duplicate payload\n")
    (project / "first.txt").write_text("first unique document\n")
    (project / "second.txt").write_text("second unique document\n")

    scanner = DriveScanner(database, config)
    scanner.scan(root)
    run_content_analysis(database, config, "documents")
    run_exact_duplicate_analysis(database, config)
    run_project_analysis(database, config)

    content_ids = [
        int(row["id"])
        for row in database.fetch_all("SELECT id FROM current_content_objects ORDER BY id")
    ]
    assert len(content_ids) >= 3
    replace_relationship_group(
        database,
        "DOCUMENT_FAMILY",
        "retirement-family",
        content_ids[:2],
        {"member_count": 2},
    )
    upsert_content_relationship(
        database,
        "CONTENT_OBJECT",
        content_ids[0],
        "CONTENT_OBJECT",
        content_ids[1],
        "TEXTUALLY_SIMILAR",
        "TIER_3_SEMANTIC_EXACT",
        0.9,
        "retirement-test",
        "1",
        "test",
        {},
        "current only while both payloads are reachable",
    )
    database.connect().execute(
        """INSERT INTO content_overlap_results(
             content_object_a_id,content_object_b_id,chunking_profile_id,shared_chunk_count,
             shared_chunk_bytes,a_total_chunk_bytes,b_total_chunk_bytes,overlap_a_in_b,
             overlap_b_in_a,weighted_jaccard,confidence)
           VALUES(?,?,1,1,10,20,20,0.5,0.5,0.5,0.5)""",
        (content_ids[0], content_ids[1]),
    )
    entry_id = int(database.fetch_one("SELECT MIN(id) id FROM current_entries")["id"])
    database.connect().execute(
        "INSERT INTO collection_clusters(cluster_type,name) VALUES('EVENT','retirement-event')"
    )
    cluster_id = int(
        database.fetch_one("SELECT id FROM collection_clusters WHERE name='retirement-event'")["id"]
    )
    database.connect().execute(
        "INSERT INTO collection_members(cluster_id,member_type,member_id) VALUES(?,'ENTRY',?)",
        (cluster_id, entry_id),
    )
    database.connect().execute(
        "INSERT INTO record_series(name) VALUES('retirement-series')"
    )
    series_id = int(
        database.fetch_one("SELECT id FROM record_series WHERE name='retirement-series'")["id"]
    )
    database.connect().execute(
        "INSERT INTO record_series_assignments(target_type,target_id,series_id) VALUES('ENTRY',?,?)",
        (entry_id, series_id),
    )
    database.connect().execute(
        "INSERT INTO preservation_assessments(target_type,target_id) VALUES('ENTRY',?)",
        (entry_id,),
    )
    database.connect().commit()

    for relation in (
        "current_content_objects",
        "current_analysis_artifacts",
        "current_exact_duplicate_groups",
        "current_projects",
        "current_relationship_groups",
        "current_content_relationships",
        "current_content_overlap_results",
        "current_collection_clusters",
        "current_record_series_assignments",
        "current_preservation_assessments",
    ):
        assert database.fetch_one(f"SELECT COUNT(*) n FROM {relation}")["n"] > 0, relation

    for path in project.iterdir():
        path.unlink()
    project.rmdir()
    scanner.scan(root)
    # Re-run the two stages whose historical tables originally produced the ghost rows.
    run_exact_duplicate_analysis(database, config)
    run_project_analysis(database, config)
    current_run = int(
        database.fetch_one("SELECT MAX(id) id FROM scan_runs WHERE status='COMPLETE'")["id"]
    )
    database.refresh_materialized_summaries(current_run)

    # The audit trail remains intact.
    assert database.fetch_one("SELECT COUNT(*) n FROM filesystem_entries")["n"] > 0
    assert database.fetch_one("SELECT COUNT(*) n FROM content_objects")["n"] > 0
    assert database.fetch_one("SELECT COUNT(*) n FROM analysis_artifacts")["n"] > 0
    assert database.fetch_one("SELECT COUNT(*) n FROM exact_duplicate_groups")["n"] > 0
    assert database.fetch_one("SELECT COUNT(*) n FROM projects")["n"] > 0
    assert database.fetch_one("SELECT COUNT(*) n FROM relationship_groups")["n"] > 0

    for relation in (
        "current_entries",
        "current_content_objects",
        "current_analysis_artifacts",
        "current_exact_duplicate_groups",
        "current_projects",
        "current_relationship_groups",
        "current_content_relationships",
        "current_content_overlap_results",
        "current_collection_clusters",
        "current_record_series_assignments",
        "current_preservation_assessments",
    ):
        assert database.fetch_one(f"SELECT COUNT(*) n FROM {relation}")["n"] == 0, relation

    summary = build_summary_context(database, config)
    assert summary["content_objects"] == 0
    assert summary["exact_duplicate_groups"] == 0
    assert summary["projects"] == 0
    assert summary["document_version_groups"] == 0
    assert summary["content_relationships"] == []
    assert build_duplicates_context(database, config)["groups"] == []
    assert build_projects_context(database, config)["projects"] == []
    assert build_document_versions_context(database, config)["families"] == []
    assert build_image_groups_context(database, config)["perceptual_groups"] == []
    assert build_errors_context(database, config)["parser_errors"] == []

    overview, _ = _summary(database, "overview")
    assert overview["entries"] == 0
    assert overview["content_objects"] == 0
    assert overview["unique_content_bytes"] == 0
    assert overview["analysis_artifacts"] == 0
    assert overview["duplicate_groups"] == 0

    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from housekeeper.dashboard.app import create_app

    client = TestClient(create_app(database, config=config))
    assert client.get("/api/overview").json()["content_objects"] == 0
    assert client.get("/api/duplicates").json() == []
    assert "retired-project" not in client.get("/projects").text
