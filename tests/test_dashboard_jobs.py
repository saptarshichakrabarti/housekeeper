"""The Jobs surface must tell the truth: a stopped job never animates or shows a live rate, its
controls match what its status actually supports, and the fragment reaps orphans as it polls.
"""

import pytest

from housekeeper.jobs import create_job, request_pause, update_job


def _pipeline(database):
    parent = create_job(database, "QUICKSTART")
    update_job(database, parent, "RUNNING")
    child = create_job(database, "SCAN", parent_job_id=parent)
    update_job(database, child, "RUNNING")
    return parent, child

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
    # The rows only: the fragment's filter form is full of `</select>`, which contains "/s".
    row_html = client.get("/fragments/jobs").text.split("<tbody>", 1)[1]
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


def test_cancel_on_stage_row_cancels_the_whole_run(client, database):
    # The Jobs table shows one row per stage of a pipeline run. Cancelling the row that happens
    # to be running must stop the RUN: the request escalates to the pipeline root job.
    parent, child = _pipeline(database)
    resp = client.post(
        f"/fragments/jobs/{child}/control?action=cancel",
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert resp.status_code == 200
    root_status = database.fetch_one("SELECT status FROM jobs WHERE id=?", (parent,))["status"]
    assert root_status == "CANCELLING"


def test_pause_on_stage_row_pauses_the_whole_run(client, database):
    parent, child = _pipeline(database)
    resp = client.post(
        f"/fragments/jobs/{child}/control?action=pause",
        headers={"X-CSRF-Token": _csrf(client)},
    )
    assert resp.status_code == 200
    root_status = database.fetch_one("SELECT status FROM jobs WHERE id=?", (parent,))["status"]
    assert root_status == "PAUSING"


def test_stop_controls_replace_the_row_they_target(client, database):
    # The endpoint answers with a whole <tr>. htmx's default innerHTML nested that inside the row it
    # was meant to replace, leaving the row's own status text on screen — the click looked dead.
    job_id = create_job(database, "SCAN")
    update_job(database, job_id, "RUNNING")
    html = client.get("/fragments/jobs").text
    for action in ("pause", "cancel"):
        marker = f"/fragments/jobs/{job_id}/control?action={action}'"
        assert f"{marker} hx-target='closest tr' hx-swap='outerHTML'" in html


def test_row_shows_stopping_while_the_request_is_still_out_of_band(client, database):
    # A cancel that could not take the write lock lives in a file until the worker settles the row.
    # The table must reflect it, or an accepted request reads as a button that did nothing.
    from housekeeper.jobs import control_path

    job_id = create_job(database, "SCAN")
    update_job(database, job_id, "RUNNING", processed_count=3)
    control_path(database, job_id).write_text("CANCELLING", encoding="utf-8")
    body = client.get("/fragments/jobs").text.split("<tbody>", 1)[1]
    assert "stopping…" in body
    assert f"/fragments/jobs/{job_id}/control?action=pause" not in body  # already stopping


def test_stage_rows_are_marked_as_part_of_their_run(client, database):
    parent, _child = _pipeline(database)
    html = client.get("/fragments/jobs").text
    assert f"stage of job #{parent}" in html
    assert "↳" in html


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


def _paused_quickstart(database, source_root="/tmp/drive"):
    import json

    job_id = create_job(database, "QUICKSTART", {"source_root": source_root})
    update_job(database, job_id, "RUNNING")
    update_job(database, job_id, "PAUSED")
    assert json.loads(
        database.fetch_one("SELECT scope_json FROM jobs WHERE id=?", (job_id,))["scope_json"]
    )["source_root"] == source_root
    return job_id


def test_paused_pipeline_offers_resume(client, database):
    job_id = _paused_quickstart(database)
    html = client.get("/fragments/jobs").text
    assert f"/fragments/jobs/{job_id}/control?action=resume" in html
    # Cancel stays: a paused run must always be stoppable for good.
    assert f"/fragments/jobs/{job_id}/control?action=cancel" in html


def test_interrupted_and_failed_pipelines_offer_resume(client, database):
    for status in ("INTERRUPTED", "FAILED", "CANCELLED"):
        job_id = create_job(database, "QUICKSTART", {"source_root": "/tmp/drive"})
        update_job(database, job_id, "RUNNING")
        update_job(database, job_id, status)
        assert f"/fragments/jobs/{job_id}/control?action=resume" in client.get("/fragments/jobs").text


def test_completed_job_offers_no_resume(client, database):
    job_id = create_job(database, "QUICKSTART", {"source_root": "/tmp/drive"})
    update_job(database, job_id, "COMPLETED")
    assert f"/fragments/jobs/{job_id}/control" not in client.get("/fragments/jobs").text
    resp = client.post(
        f"/fragments/jobs/{job_id}/control?action=resume", headers={"X-CSRF-Token": _csrf(client)}
    )
    assert resp.status_code == 422


def test_stage_row_offers_no_resume_of_its_own(client, database):
    parent, child = _pipeline(database)
    update_job(database, parent, "INTERRUPTED")
    update_job(database, child, "INTERRUPTED")
    html = client.get("/fragments/jobs").text
    # The run is resumable; the stage is resumed through it, not on its own.
    assert f"/fragments/jobs/{parent}/control?action=resume" in html
    assert f"/fragments/jobs/{child}/control?action=resume" not in html


def test_read_only_dashboard_never_offers_resume(config, database):
    from fastapi.testclient import TestClient

    from housekeeper.dashboard.app import create_app

    _paused_quickstart(database)
    viewer = TestClient(create_app(database, read_only=True, config=config))
    assert "action=resume" not in viewer.get("/fragments/jobs").text


def test_resume_starts_a_new_pipeline_linked_to_the_old_one(client, database, tmp_path):
    import json

    source = tmp_path / "drive"
    (source / "sub").mkdir(parents=True)
    (source / "sub" / "a.txt").write_text("content", encoding="utf-8")
    old = _paused_quickstart(database, str(source))
    resp = client.post(
        f"/fragments/jobs/{old}/control?action=resume", headers={"X-CSRF-Token": _csrf(client)}
    )
    assert resp.status_code == 200
    _wait_for_new_quickstart(database, old)
    row = database.fetch_one(
        "SELECT id,scope_json FROM jobs WHERE job_type='QUICKSTART' AND id<>? ORDER BY id DESC LIMIT 1",
        (old,),
    )
    assert json.loads(row["scope_json"])["resumes"] == old
    # The old row is left terminal: what happened to it is a fact, not something a resume rewrites.
    assert database.fetch_one("SELECT status FROM jobs WHERE id=?", (old,))["status"] == "PAUSED"


def _wait_for_new_quickstart(database, old_id, timeout=30):
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        row = database.fetch_one(
            "SELECT status FROM jobs WHERE job_type='QUICKSTART' AND id<>? ORDER BY id DESC LIMIT 1",
            (old_id,),
        )
        if row and row["status"] in {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED"}:
            return
        time.sleep(0.05)
    raise TimeoutError("the resumed pipeline did not finish in time")


def test_jobs_filter_narrows_by_type_and_status(client, database):
    scan = create_job(database, "SCAN")
    update_job(database, scan, "COMPLETED")
    classification = create_job(database, "CLASSIFICATION")
    update_job(database, classification, "FAILED")
    only_scan = client.get("/fragments/jobs?job_type=SCAN").text.split("<tbody>", 1)[1]
    assert "SCAN" in only_scan and "CLASSIFICATION" not in only_scan
    only_failed = client.get("/fragments/jobs?status=FAILED").text.split("<tbody>", 1)[1]
    assert "CLASSIFICATION" in only_failed and "SCAN" not in only_failed
    # The filter survives the poll, so a refresh cannot silently widen the list.
    assert "job_type=SCAN" in client.get("/fragments/jobs?job_type=SCAN").text


def test_pipelines_only_filter_hides_stages(client, database):
    parent, child = _pipeline(database)
    rows = client.get("/fragments/jobs?pipelines_only=1").text.split("<tbody>", 1)[1]
    assert f"<td>{parent}</td>" in rows
    assert f"<td>{child}</td>" not in rows


def test_durations_are_shown_for_finished_and_running_jobs(client, database):
    finished = create_job(database, "SCAN")
    database.connect().execute(
        "UPDATE jobs SET status='COMPLETED',started_at='2024-01-01 00:00:00',"
        "completed_at='2024-01-01 00:02:30' WHERE id=?",
        (finished,),
    )
    database.connect().commit()
    assert "02:30" in client.get("/fragments/jobs").text
    running = create_job(database, "SCAN")
    update_job(database, running, "RUNNING")
    assert "elapsed" in client.get("/fragments/jobs").text


def test_pipeline_root_expands_to_its_stages(client, database):
    parent, child = _pipeline(database)
    assert f"/fragments/jobs/{parent}/stages" in client.get("/fragments/jobs").text
    stages = client.get(f"/fragments/jobs/{parent}/stages").text
    assert f"<td>{child}</td>" in stages and "SCAN" in stages
    # A job with no children says so rather than rendering an empty table.
    assert "No stages recorded" in client.get(f"/fragments/jobs/{child}/stages").text


def test_stages_expand_in_place_and_collapse(client, database):
    # The button used to insert a row after the pipeline's own (hx-swap=afterend), so every click
    # appended another copy of the same table. It now replaces one row identified by the run.
    parent, _child = _pipeline(database)
    table = client.get("/fragments/jobs").text
    assert f"hx-target='#job-stages-{parent}' hx-swap='outerHTML'" in table
    assert f"<tr id='job-stages-{parent}' class='stages-row' hidden></tr>" in table  # the row the click replaces

    stages = client.get(f"/fragments/jobs/{parent}/stages").text
    assert stages.startswith(f"<tr id='job-stages-{parent}' class='stages-row'>")  # same row, so it cannot stack
    assert f"/fragments/jobs/{parent}/stages?expanded=0" in stages  # and it can be collapsed again
    assert client.get(f"/fragments/jobs/{parent}/stages?expanded=0").text == (
        f"<tr id='job-stages-{parent}' class='stages-row' hidden></tr>"
    )
