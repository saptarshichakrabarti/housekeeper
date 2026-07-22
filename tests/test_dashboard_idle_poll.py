"""The jobs poll self-suspends when idle and resumes when a job starts.

Acceptance: an idle dashboard issues no repeating job queries (no `every Ns` trigger), and the
control endpoints emit an `HX-Trigger: job-started` header so a suspended poll wakes back up.
"""

import time

import pytest

from housekeeper.jobs import create_job, update_job

pytest.importorskip("fastapi")
pytest.importorskip("httpx")


@pytest.fixture
def client(config, database):
    from fastapi.testclient import TestClient
    from housekeeper.dashboard.app import create_app

    return TestClient(create_app(database, config=config))


def test_idle_jobs_fragment_has_no_repeating_poll(client):
    html = client.get("/fragments/jobs").text
    assert "every 3s" not in html
    assert "job-started from:body" in html  # only re-arms on an explicit job start


def test_active_jobs_fragment_keeps_polling(client, database):
    job_id = create_job(database, "SCAN")
    update_job(database, job_id, "RUNNING")
    html = client.get("/fragments/jobs").text
    assert "every 3s" in html


def test_jobs_page_only_loads_once(client):
    # The page no longer hard-codes a forever poll; the fragment decides the cadence.
    assert "every 3s" not in client.get("/jobs").text


def test_control_scan_emits_job_started_header(client, tmp_path):
    source = tmp_path / "src"
    source.mkdir()
    (source / "a.txt").write_text("hello", encoding="utf-8")
    token = client.get("/api/csrf").json()["token"]
    resp = client.post(
        "/control/scan", data={"path": str(source)}, headers={"X-CSRF-Token": token}
    )
    assert resp.status_code == 200
    assert resp.headers.get("HX-Trigger") == "job-started"
    # Let the background worker settle so it does not outlive the test.
    deadline = time.monotonic() + 15
    while "running" in client.get("/fragments/control").text:
        if time.monotonic() > deadline:
            raise TimeoutError("scan did not finish")
        time.sleep(0.05)
