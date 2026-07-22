"""The overview is served from materialized summaries — no full-table scan on a normal load."""

import json

import pytest

from housekeeper.dashboard.services import DashboardService
from housekeeper.scanner import DriveScanner

# Tables whose presence in an overview query means we are scanning the inventory live.
_FULL_SCAN_TABLES = (
    "filesystem_entries",
    "content_objects",
    "classifications",
    "exact_duplicate_groups",
    "analysis_artifacts",
    "scan_runs",
)


def _scan(database, config, fixture_root):
    DriveScanner(database, config).scan(fixture_root, incremental=False)


def test_refresh_stores_charts_and_folded_counts(database, config, fixture_root):
    _scan(database, config, fixture_root)
    charts = json.loads(
        database.fetch_one(
            "SELECT value_json FROM materialized_summaries WHERE summary_key='charts'"
        )["value_json"]
    )
    assert set(charts) == {
        "file_types",
        "classification_bytes",
        "top_level",
        "scan_history",
        "analyser_completion",
    }
    overview = json.loads(
        database.fetch_one(
            "SELECT value_json FROM materialized_summaries WHERE summary_key='overview'"
        )["value_json"]
    )
    # database_stats' three COUNTs are now folded in (item 6).
    real = database.fetch_one("SELECT COUNT(*) n FROM filesystem_entries")["n"]
    assert overview["entries"] == real
    assert "content_objects" in overview
    assert "analysis_artifacts" in overview


def test_overview_issues_no_full_table_scan(database, config, fixture_root):
    _scan(database, config, fixture_root)
    service = DashboardService(database.reader())
    executed: list[str] = []
    database._read_conn().set_trace_callback(executed.append)
    try:
        model = service.overview()
    finally:
        database._read_conn().set_trace_callback(None)
    joined = " ".join(executed).lower()
    for table in _FULL_SCAN_TABLES:
        assert f"from {table}" not in joined and f"join {table}" not in joined, (
            f"overview scanned {table} live: {executed}"
        )
    assert model.charts, "charts should be served from the materialized summaries"
    assert model.refreshed_at is not None


def test_overview_metrics_match_live_counts(database, config, fixture_root):
    _scan(database, config, fixture_root)
    model = DashboardService(database.reader()).overview()
    metrics = {metric.label: metric.value for metric in model.metrics}
    assert metrics["Entries"] == database.fetch_one("SELECT COUNT(*) n FROM filesystem_entries")["n"]
    assert (
        metrics["Content objects"]
        == database.fetch_one("SELECT COUNT(*) n FROM content_objects")["n"]
    )


def test_overview_view_model_is_ttl_cached(database, config, fixture_root):
    _scan(database, config, fixture_root)
    service = DashboardService(database.reader())
    first = service.overview()
    assert service.overview() is first  # served from the in-process TTL cache
    service.invalidate_overview()
    assert service.overview() is not first


def test_overview_before_any_refresh_is_empty_not_a_crash(database):
    # A brand-new database has no summaries yet: the overview must render zeros, not scan or fail.
    model = DashboardService(database.reader()).overview()
    assert model.refreshed_at is None
    assert {metric.value for metric in model.metrics} == {0}
    assert model.charts == ()


@pytest.fixture
def client(config, database):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from housekeeper.dashboard.app import create_app

    return TestClient(create_app(database, config=config))


def test_refresh_endpoint_repopulates_summaries(client, database, config, fixture_root):
    _scan(database, config, fixture_root)
    database.connect().execute("DELETE FROM materialized_summaries")
    database.connect().commit()
    token = client.get("/api/csrf").json()["token"]
    resp = client.post("/refresh", headers={"X-CSRF-Token": token})
    assert resp.status_code == 200
    assert (
        database.fetch_one(
            "SELECT COUNT(*) n FROM materialized_summaries WHERE summary_key='charts'"
        )["n"]
        == 1
    )


def test_refresh_requires_csrf(client):
    assert client.post("/refresh").status_code == 403


def test_refresh_blocked_on_read_only_dashboard(database):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from housekeeper.dashboard.app import create_app

    ro = TestClient(create_app(database, read_only=True))
    token = ro.get("/api/csrf").json()["token"]
    assert ro.post("/refresh", headers={"X-CSRF-Token": token}).status_code == 403
