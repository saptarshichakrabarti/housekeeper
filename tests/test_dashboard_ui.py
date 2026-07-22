"""Legibility and navigation contracts for the server-rendered dashboard."""

from datetime import UTC, datetime

import pytest

from housekeeper.dashboard.filters import filesizeformat, relativetime, thousands


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
        "'report.pdf','.pdf','file',12400,1721642400)"
    )
    conn.execute(
        "INSERT INTO content_objects(id,hash_algorithm,full_hash,size_bytes) "
        "VALUES(1,'sha256','unique',2400)"
    )
    conn.execute(
        "INSERT INTO exact_duplicate_groups(id,full_hash,size_bytes,member_count) VALUES"
        "(1,'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa',1000,3),"
        "(2,'bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb',5000,2)"
    )
    conn.commit()
    database.refresh_materialized_summaries()
    return TestClient(create_app(database))


def test_overview_has_grouped_navigation_hero_links_and_bars(dashboard_client) -> None:
    body = dashboard_client.get("/").text
    assert "Reclaimable space" in body
    assert 'class="hero-card card card--ok" href="/duplicates"' in body
    assert 'class="nav-heading">Review' in body
    assert 'class="nav-heading">Insights' in body
    assert 'class="nav-heading">System' in body
    assert 'class="nav-separator" aria-hidden="true">·</span>' in body
    assert 'href="/" aria-current="page"' in body
    assert 'class="bar"' in body
    assert 'href="/duplicates"' in body


def test_review_renders_filter_bar_chips_and_human_values(dashboard_client) -> None:
    body = dashboard_client.get("/review?extension=.pdf&minimum_size=1000").text
    assert 'class="filter-bar"' in body
    assert 'name="top_level_directory"' in body
    assert 'name="duplicate_only"' in body
    assert 'class="filter-chip"' in body
    assert "12.4 KB" in body
    assert 'title="12,400 bytes"' in body
    assert 'href="/review" aria-current="page"' in body
    assert "Keyboard shortcuts" in body


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
