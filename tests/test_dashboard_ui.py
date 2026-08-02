"""Legibility and navigation contracts for the server-rendered dashboard."""

from datetime import UTC, datetime

import pytest

from housekeeper.dashboard.filters import (
    classification_label,
    decision_label,
    filesizeformat,
    reason_labels,
    relativetime,
    thousands,
)


def test_human_readable_filters_keep_exact_values() -> None:
    rendered = str(filesizeformat(12_400_000_000))
    assert "12.4 GB" in rendered
    assert 'title="12,400,000,000 bytes"' in rendered
    assert 'data-bytes="12400000000"' in rendered
    assert thousands(243485) == "243,485"

    relative = str(
        relativetime(
            datetime(2026, 7, 22, 10, tzinfo=UTC),
            now=datetime(2026, 7, 22, 12, tzinfo=UTC),
        )
    )
    assert "2 h ago" in relative
    assert 'datetime="2026-07-22T10:00:00Z"' in relative


def test_enum_values_render_as_readable_labels() -> None:
    assert classification_label("REVIEW_SAFE") == "Safe to review"
    assert classification_label("PROTECTED") == "Protected"
    assert decision_label("NEEDS_MORE_ANALYSIS") == "Needs more analysis"
    assert decision_label("MARK_KEEP") == "Keep"
    # Unmapped codes still humanise generically rather than showing SCREAMING_SNAKE_CASE.
    assert classification_label("REVIEW_BRAND_NEW") == "Review brand new"
    assert classification_label("") == "" and decision_label(None) == ""


def test_reason_codes_json_becomes_readable_labels() -> None:
    assert reason_labels('["OLD_AND_DUPLICATED","REGENERABLE"]') == [
        "Old and duplicated",
        "Regenerable",
    ]
    assert reason_labels('["NODE_MODULES"]') == ["node_modules"]  # override, not "Node modules"
    assert reason_labels("") == [] and reason_labels("not json") == []


@pytest.fixture
def dashboard_client(database):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from housekeeper.dashboard.app import create_app

    conn = database.connect()
    conn.execute(
        "INSERT INTO scan_runs(id,source_root,source_root_fingerprint,status) "
        "VALUES(1,'/drive','drive','COMPLETE')"
    )
    conn.execute(
        "INSERT INTO filesystem_entries(id,scan_run_id,source_root,absolute_path,relative_path,"
        "name,suffix,entry_type,size_bytes,modified_at) "
        "VALUES(1,1,'/drive','/drive/Documents/report.pdf','Documents/report.pdf',"
        "'report.pdf','.pdf','file',12400,1721642400),"
        "(2,1,'/drive','/drive/a-1.bin','a-1.bin','a-1.bin','.bin','file',1000,1),"
        "(3,1,'/drive','/drive/a-2.bin','a-2.bin','a-2.bin','.bin','file',1000,1),"
        "(4,1,'/drive','/drive/a-3.bin','a-3.bin','a-3.bin','.bin','file',1000,1),"
        "(5,1,'/drive','/drive/b-1.bin','b-1.bin','b-1.bin','.bin','file',5000,1),"
        "(6,1,'/drive','/drive/b-2.bin','b-2.bin','b-2.bin','.bin','file',5000,1)"
    )
    conn.execute(
        "INSERT INTO content_objects(id,hash_algorithm,full_hash,size_bytes) "
        "VALUES(1,'sha256','unique',2400)"
    )
    conn.execute(
        "INSERT INTO entry_content_links(entry_id,content_object_id,link_status) "
        "VALUES(1,1,'VERIFIED')"
    )
    conn.execute(
        "INSERT INTO exact_duplicate_groups(id,full_hash,size_bytes,member_count,canonical_entry_id) VALUES"
        "(1,'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',1000,3,2),"
        "(2,'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',5000,2,5)"
    )
    conn.execute(
        "INSERT INTO exact_duplicate_members(group_id,entry_id,is_canonical,readable) VALUES"
        "(1,2,1,1),(1,3,0,1),(1,4,0,1),(2,5,1,1),(2,6,0,1)"
    )
    database.refresh_current_inventory_views()  # the scanner does this; a raw-SQL seed must too
    conn.commit()
    database.refresh_materialized_summaries()
    return TestClient(create_app(database))


def test_overview_has_grouped_navigation_hero_links_and_bars(dashboard_client) -> None:
    body = dashboard_client.get("/").text
    assert "Reclaimable space" in body
    assert 'class="hero-card card card--ok" href="/duplicates"' in body
    assert 'class="cards overview-metrics"' in body
    assert body.count('class="card-description"') == 12
    assert "Review candidates" in body
    assert "Items suggested for review" in body
    assert "Items classified to retain" in body
    assert "Items needing attention" in body
    assert 'class="nav-heading">Review' in body
    assert 'class="nav-heading">Insights' in body
    assert 'class="nav-heading">System' in body
    assert 'class="nav-separator" aria-hidden="true">·</span>' in body
    assert 'href="/" aria-current="page"' in body
    assert 'class="bar"' in body
    assert 'href="/duplicates"' in body


def test_review_renders_filter_bar_chips_and_human_values(dashboard_client) -> None:
    # show_all: the sample row is an unclassified pdf; the default actionable queue would hide it.
    body = dashboard_client.get("/review?extension=.pdf&minimum_size=1000&show_all=true").text
    assert 'class="filter-bar"' in body
    assert 'name="top_level_directory"' in body
    assert 'name="duplicate_only"' in body
    assert 'class="filter-chip"' in body
    assert "12.4 KB" in body
    assert 'title="12,400 bytes"' in body
    assert 'href="/review" aria-current="page"' in body
    assert "Keyboard shortcuts" in body


def _review_client(database):
    from fastapi.testclient import TestClient

    from housekeeper.dashboard.app import create_app

    conn = database.connect()
    conn.execute(
        "INSERT INTO scan_runs(id,source_root,source_root_fingerprint,status) "
        "VALUES(1,'/drive','drive','COMPLETE')"
    )
    conn.execute(
        "INSERT INTO filesystem_entries(id,scan_run_id,source_root,absolute_path,relative_path,name,entry_type,size_bytes) VALUES"
        "(10,1,'/drive','/drive/undecided.bin','undecided.bin','undecided.bin','file',1000),"
        "(11,1,'/drive','/drive/decided.bin','decided.bin','decided.bin','file',1000),"
        "(12,1,'/drive','/drive/kept.bin','kept.bin','kept.bin','file',1000)"
    )
    conn.execute(
        "INSERT INTO classifications(entry_id,classification,confidence,reason_codes_json) VALUES"
        "(10,'REVIEW_SAFE',0.9,'[\"OLD_AND_DUPLICATED\"]'),(11,'REVIEW_SAFE',0.9,'[]'),(12,'KEEP',0.9,'[]')"
    )
    conn.execute("INSERT INTO review_sessions(id,name,status) VALUES(1,'S','OPEN')")
    conn.execute(
        "INSERT INTO review_decisions(review_session_id,target_type,target_id,decision,current) "
        "VALUES(1,'ENTRY',11,'MARK_KEEP',1)"
    )
    database.refresh_current_inventory_views()
    conn.commit()
    return TestClient(create_app(database))


def test_review_defaults_to_the_actionable_queue(database) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    client = _review_client(database)
    default = client.get("/review").text
    # Assert on the row's entry link, not the bare name (names also appear in filter dropdowns).
    assert "/fragments/entry/10" in default  # the undecided review candidate is queued
    assert "/fragments/entry/11" not in default  # already has a decision
    assert "/fragments/entry/12" not in default  # KEEP, not a review candidate
    assert "Items that need review" in default
    assert "Show all files" in default
    # Enum/JSON columns render as readable labels, with the raw code kept in a tooltip.
    assert "Safe to review" in default
    assert 'title="REVIEW_SAFE"' in default
    assert "Old and duplicated" in default
    assert "REVIEW_SAFE</td>" not in default  # never the bare code as the cell's text


def test_review_show_all_reveals_the_full_inventory(database) -> None:
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    client = _review_client(database)
    body = client.get("/review?show_all=true").text
    assert "undecided.bin" in body and "decided.bin" in body and "kept.bin" in body
    assert "All files" in body
    # The "all files" scope stays sticky across pagination.
    assert "show_all=true" in body or "Next page" not in body


def test_detail_drawer_is_a_fixed_side_panel(dashboard_client) -> None:
    # The drawer target lives inside a fixed .detail-panel with its own close control and backdrop,
    # so opening a detail overlays the page rather than reflowing the table beneath it.
    body = dashboard_client.get("/review").text
    panel_start = body.index('class="detail-panel"')
    assert 'id="detail-backdrop"' in body
    assert 'class="detail-panel-close"' in body
    # The htmx swap target is nested within the panel (so swaps fill the panel, not the page flow).
    assert body.index('id="detail-drawer"') > panel_start
    # Both review and duplicates share the pattern.
    assert 'class="detail-panel"' in dashboard_client.get("/duplicates").text


def test_duplicates_lead_with_reclaimable_space_and_copyable_hash(dashboard_client) -> None:
    body = dashboard_client.get("/duplicates").text
    assert body.index("5.0 KB") < body.index("2.0 KB")
    assert "Reclaimable" in body
    assert 'data-copy="aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"' in body
    assert "aaaaaaaaaaaa…" in body


def test_global_prefix_search_is_read_only_and_formatted(dashboard_client) -> None:
    page = dashboard_client.get("/").text
    assert 'role="search"' in page
    assert 'action="/search"' in page
    results = dashboard_client.get("/search?q=Documents").text
    assert "Documents/report.pdf" in results
    assert "12.4 KB" in results
    assert "prefix-based and read-only" in results
    # Results are inspectable via the shared drawer, not inert text.
    assert 'hx-get="/fragments/entry/1"' in results
    assert 'id="detail-panel"' in results


def test_search_states_when_results_are_truncated(dashboard_client) -> None:
    # Three a-*.bin entries exist; a limit of two must say so rather than silently dropping one.
    truncated = dashboard_client.get("/search?q=a-&limit=2").text
    assert "Showing the first 2 matches" in truncated
    assert "Refine the prefix" in truncated
    # Under the limit, it reports an exact count with no truncation note.
    exact = dashboard_client.get("/search?q=Documents").text
    assert "1 match for" in exact
    assert "Refine the prefix" not in exact
