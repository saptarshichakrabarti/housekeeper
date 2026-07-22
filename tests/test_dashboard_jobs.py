"""The Jobs surface must tell the truth: a stopped job never animates or shows a live rate, its
controls match what its status actually supports, and the fragment reaps orphans as it polls.
"""

import pytest

from housekeeper.jobs import create_job, request_pause, update_job

pytest.importorskip("fastapi")
pytest.importorskip("httpx")


@pytest.fixture
def client(config, database):
    from fastapi.testclient import TestClient
    from housekeeper.dashboard.app import create_app

    # config= (not read-only) so the operational controls and the reaper are active.
    return TestClient(create_app(database, config=config))


def _csrf(client):
    return client.get("/api/csrf").json()["token"]


def test_running_job_shows_live_bar_and_rate(client, database):
    job_id = create_job(database, "SCAN")
    update_job(database, job_id, "RUNNING", processed_count=10)
    html = client.get("/fragments/jobs").text
    assert "<progress></progress>" in html or "<progress " in html
    assert "/s" in html  # a rate is shown while genuinely running
    # A running job offers both stop controls.
    assert f"/fragments/jobs/{job_id}/control?action=pause" in html
    assert f"/fragments/jobs/{job_id}/control?action=cancel" in html


def test_cancelled_job_is_static_and_labelled(client, database):
    job_id = create_job(database, "EXACT_DUPLICATES")
    update_job(database, job_id, "RUNNING", processed_count=25124)
    update_job(database, job_id, "CANCELLED")
    row_html = client.get("/fragments/jobs").text
    assert "cancelled · 25,124 processed" in row_html
    # No indeterminate (animated) progress element, and no fabricated throughput.
    assert "<progress></progress>" not in row_html
    assert "/s" not in row_html
    # A finished job offers no controls — nothing is left to stop.
    assert f"/fragments/jobs/{job_id}/control" not in row_html


def test_paused_job_can_still_be_cancelled(client, database):
    job_id = create_job(database, "SCAN")
    update_job(database, job_id, "RUNNING")
    request_pause(database, job_id)
    update_job(database, job_id, "PAUSED")
    html = client.get("/fragments/jobs").text
    assert f"/fragments/jobs/{job_id}/control?action=cancel" in html
    # Pause is meaningless on an already-paused job, so it is not offered.
    assert f"/fragments/jobs/{job_id}/control?action=pause" not in html


def test_control_cancel_on_paused_job_finalizes(client, database):
    job_id = create_job(database, "SCAN")
    update_job(database, job_id, "RUNNING")
    request_pause(database, job_id)
    update_job(database, job_id, "PAUSED")
    resp = client.post(
        f"/fragments/jobs/{job_id}/control?action=cancel",
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert resp.status_code == 200
    assert "cancelled" in resp.text
    assert database.fetch_one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "CANCELLED"


def test_control_pause_on_finished_job_is_harmless(client, database):
    # A double-click race: the job finished between render and click. The endpoint must not 500.
    job_id = create_job(database, "SCAN")
    update_job(database, job_id, "COMPLETED")
    resp = client.post(
        f"/fragments/jobs/{job_id}/control?action=pause",
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert resp.status_code == 200
    assert "completed" in resp.text
    assert database.fetch_one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "COMPLETED"


def test_jobs_fragment_reaps_orphan_on_poll(client, database):
    # An orphan from a dead remote worker (stale heartbeat) becomes INTERRUPTED as the page polls.
    job_id = create_job(database, "SCAN")
    update_job(database, job_id, "RUNNING")
    database.connect().execute(
        "UPDATE jobs SET host='dead-host',process_id=999999,updated_at='2000-01-01 00:00:00' WHERE id=?",
        (job_id,),
    )
    database.connect().commit()

    html = client.get("/fragments/jobs").text

    assert "interrupted" in html
    assert database.fetch_one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "INTERRUPTED"


def test_operational_jobs_page_has_a_launcher(client):
    # With the runner active, the Jobs page embeds the control panel so work can be started here.
    html = client.get("/jobs").text
    assert "id='control-panel'" in html or 'id="control-panel"' in html
    assert "folder-picker.js" in html
    # The panel's four operations are served by /fragments/control.
    fragment = client.get("/fragments/control").text
    for action in ("/control/scan", "/control/analyse", "/control/classify", "/control/report"):
        assert action in fragment


def test_plain_jobs_page_has_no_launcher(database):
    # A viewer dashboard (no runner) shows the jobs list only — no way to start work.
    from fastapi.testclient import TestClient
    from housekeeper.dashboard.app import create_app

    viewer = TestClient(create_app(database))
    html = viewer.get("/jobs").text
    assert "control-panel" not in html
    assert "folder-picker.js" not in html


def test_read_only_dashboard_does_not_reconcile(config, database):
    """A read-only dashboard observes without mutating — it must not reap jobs."""
    from fastapi.testclient import TestClient
    from housekeeper.dashboard.app import create_app

    job_id = create_job(database, "SCAN")
    update_job(database, job_id, "RUNNING")
    database.connect().execute(
        "UPDATE jobs SET host='dead-host',process_id=999999,updated_at='2000-01-01 00:00:00' WHERE id=?",
        (job_id,),
    )
    database.connect().commit()

    ro_client = TestClient(create_app(database, read_only=True))
    ro_client.get("/fragments/jobs")
    assert database.fetch_one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "RUNNING"
