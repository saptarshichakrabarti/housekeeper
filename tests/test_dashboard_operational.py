"""Operational GUI: the background runner and the /control/* endpoints it powers.

Every operation here is read-only w.r.t. the source drive (scan/analyse/classify/report) — there
is no move/delete endpoint, mirroring the CLI's own safety split (moving stays a separate,
explicit, manifest-verified flow: export-review -> validate-manifest -> move-to-review).
"""

import threading
import time

import pytest

from housekeeper.dashboard.runner import OperationRunner
from housekeeper.database import Database


def _wait_idle(runner, timeout=15):
    deadline = time.monotonic() + timeout
    while runner.status()["state"] == "running":
        if time.monotonic() > deadline:
            raise TimeoutError("operation did not finish in time")
        time.sleep(0.02)


def _wait_idle_via_http(client, timeout=15):
    deadline = time.monotonic() + timeout
    while "running" in client.get("/fragments/control").text:
        if time.monotonic() > deadline:
            raise TimeoutError("operation did not finish in time")
        time.sleep(0.05)


def test_submit_runs_quickstart_against_a_temp_workspace(config, fixture_root):
    runner = OperationRunner(config)
    assert runner.submit("quickstart", source=str(fixture_root)) == "accepted"
    _wait_idle(runner)
    assert runner.status()["state"] == "idle"
    db = Database(config.database_path)
    db.initialize()
    try:
        assert db.fetch_one("SELECT COUNT(*) n FROM filesystem_entries")["n"] > 0
    finally:
        db.close()


def test_submit_purge_clears_a_scanned_workspace(config, fixture_root):
    runner = OperationRunner(config)
    assert runner.submit("quickstart", source=str(fixture_root)) == "accepted"
    _wait_idle(runner)
    assert runner.submit("purge") == "accepted"
    _wait_idle(runner)
    assert runner.status()["state"] == "idle"
    db = Database(config.database_path)
    db.initialize()
    try:
        assert db.fetch_one("SELECT COUNT(*) n FROM filesystem_entries")["n"] == 0
        # The purge is a tracked job like everything else, and the one job row that survives it is
        # its own: history is gone, but the fact that it was purged is not.
        jobs = db.fetch_all("SELECT job_type,status FROM jobs")
        assert [(row["job_type"], row["status"]) for row in jobs] == [("PURGE", "COMPLETED")]
    finally:
        db.close()


def test_submit_rejects_a_concurrent_operation(config, monkeypatch):
    started = threading.Event()
    release = threading.Event()

    def slow(self, database):
        started.set()
        release.wait(timeout=5)

    monkeypatch.setattr(OperationRunner, "_run_classify", slow)
    runner = OperationRunner(config)
    assert runner.submit("classify") == "accepted"
    assert started.wait(timeout=5)
    assert runner.submit("classify") == "busy"
    release.set()
    _wait_idle(runner)


def test_submit_raising_operation_becomes_error_without_crashing(config, monkeypatch):
    def boom(self, database):
        raise RuntimeError("synthetic failure")

    monkeypatch.setattr(OperationRunner, "_run_classify", boom)
    runner = OperationRunner(config)
    assert runner.submit("classify") == "accepted"
    _wait_idle(runner)
    status = runner.status()
    assert status["state"] == "error"
    assert "synthetic failure" in status["error"]


def test_cancelled_operation_reports_cancelled_not_error(config, monkeypatch):
    """A deliberate stop must not read as a failure on the Run page."""
    from housekeeper.jobs import JobCancelled

    def cancelled(self, database):
        raise JobCancelled("job 1 cancelled")

    monkeypatch.setattr(OperationRunner, "_run_classify", cancelled)
    runner = OperationRunner(config)
    assert runner.submit("classify") == "accepted"
    _wait_idle(runner)
    status = runner.status()
    assert status["state"] == "cancelled"
    assert status["error"] is None
    # A cancelled run frees the worker so the next operation can start.
    assert runner.status()["state"] != "running"


def test_paused_operation_reports_paused_not_error(config, monkeypatch):
    from housekeeper.jobs import JobPaused

    def paused(self, database):
        raise JobPaused("job 1 paused")

    monkeypatch.setattr(OperationRunner, "_run_classify", paused)
    runner = OperationRunner(config)
    assert runner.submit("classify") == "accepted"
    _wait_idle(runner)
    assert runner.status()["state"] == "paused"


@pytest.fixture
def operational_client(config, database):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from housekeeper.dashboard.app import create_app

    return TestClient(create_app(database, config=config))


def _csrf(client):
    return client.get("/api/csrf").json()["token"]


def test_control_scan_validates_and_requires_csrf(operational_client, tmp_path):
    client = operational_client
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_text("hello", encoding="utf-8")

    # No CSRF header -> 403, before any validation of the form body.
    assert client.post("/control/scan", data={"path": str(source)}).status_code == 403

    token = _csrf(client)
    headers = {"X-CSRF-Token": token}

    missing = tmp_path / "does-not-exist"
    assert client.post("/control/scan", data={"path": str(missing)}, headers=headers).status_code == 422

    a_file = tmp_path / "file.txt"
    a_file.write_text("x", encoding="utf-8")
    assert client.post("/control/scan", data={"path": str(a_file)}, headers=headers).status_code == 422

    response = client.post("/control/scan", data={"path": str(source)}, headers=headers)
    assert response.status_code == 200
    _wait_idle_via_http(client)


def test_control_analyse_classify_report_reject_bad_kind(operational_client):
    client = operational_client
    headers = {"X-CSRF-Token": _csrf(client)}
    assert (
        client.post("/control/analyse", data={"kind": "not-a-kind"}, headers=headers).status_code
        == 422
    )
    assert (
        client.post("/control/report", data={"kind": "not-a-kind"}, headers=headers).status_code
        == 422
    )


def test_control_routes_absent_under_read_only(config, database):
    """Read-only never builds the runner, so the mutating routes are not mounted at all."""
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from housekeeper.dashboard.app import create_app

    client = TestClient(create_app(database, read_only=True, config=config))
    headers = {"X-CSRF-Token": client.get("/api/csrf").json()["token"]}
    assert client.post("/control/scan", data={"path": "/"}, headers=headers).status_code == 404
    assert client.post("/control/analyse", data={"kind": "all"}, headers=headers).status_code == 404
    assert client.post("/control/classify", headers=headers).status_code == 404
    assert client.post("/control/report", data={"kind": "all"}, headers=headers).status_code == 404
    # The control page itself is gone, and nothing links to it.
    assert client.get("/control").status_code == 404
    assert 'href="/control"' not in client.get("/").text


def test_plain_dashboard_has_no_control_routes(database):
    """``create_app`` without ``config=`` (today's ``housekeeper dashboard``) is unchanged."""
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from housekeeper.dashboard.app import create_app

    client = TestClient(create_app(database))
    assert client.get("/control").status_code == 404
    assert 'href="/control"' not in client.get("/").text
