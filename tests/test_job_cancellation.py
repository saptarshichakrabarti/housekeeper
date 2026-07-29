"""Cooperative pause/cancel is now honored by every analyser, including ones that previously ran
to completion regardless of a control request.

The ``checkpoint`` helper is the single cooperative point: it honors a pending pause/cancel and
records progress, and is a no-op without a job. These tests exercise the helper directly and prove
that a representative directory-overlap job and a collections job (which had no ``job_id`` at all
before this change) both stop at their next checkpoint when paused or cancelled.
"""

import pytest

import housekeeper.jobs as jobs_module
from housekeeper.analysers.directory_overlap import run_directory_overlap_analysis
from housekeeper.analysers.exact_duplicates import run_exact_duplicate_analysis
from housekeeper.collections.events import run_acquisition_batch_analysis
from housekeeper.jobs import (
    JobCancelled,
    JobPaused,
    checkpoint,
    create_job,
    request_cancel,
    request_pause,
    tracked_job,
    update_job,
)
from housekeeper.scanner import DriveScanner


def _reset_poll_throttle():
    # check_cancelled polls at most every CANCELLATION_POLL_SECONDS per job; tests that issue a
    # request and immediately checkpoint the same job must not be suppressed by that throttle.
    jobs_module._last_poll = (None, 0.0)


def _status(database, job_id):
    return database.fetch_one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"]


def test_checkpoint_is_noop_without_job(database):
    # No job -> no error and nothing recorded; analysers stay directly callable outside a job.
    checkpoint(database, None, processed_count=5)


def test_checkpoint_honors_pause(database):
    job_id = create_job(database, "TEST")
    update_job(database, job_id, "RUNNING")
    request_pause(database, job_id)
    with pytest.raises(JobPaused):
        checkpoint(database, job_id, processed_count=1)
    assert _status(database, job_id) == "PAUSED"


def test_checkpoint_honors_cancel(database):
    job_id = create_job(database, "TEST")
    update_job(database, job_id, "RUNNING")
    request_cancel(database, job_id)
    with pytest.raises(JobCancelled):
        checkpoint(database, job_id, processed_count=1)
    assert _status(database, job_id) == "CANCELLED"


def _two_overlapping_dirs(config, database, tmp_path):
    root = tmp_path / "src"
    a = root / "A"
    b = root / "B"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    for name in ("one.txt", "two.txt", "three.txt"):
        (a / name).write_text(f"shared {name}", encoding="utf-8")
        (b / name).write_text(f"shared {name}", encoding="utf-8")
    config.section("directory_overlap")["minimum_files"] = 1
    config.section("directory_overlap")["minimum_bytes"] = 0
    DriveScanner(database, config).scan(root, incremental=False)
    run_exact_duplicate_analysis(database, config)  # hash so candidate pairs exist


def test_directory_overlap_honors_pause(config, database, tmp_path):
    _two_overlapping_dirs(config, database, tmp_path)
    job_id = create_job(database, "DIRECTORY_OVERLAP")
    update_job(database, job_id, "RUNNING")
    request_pause(database, job_id)
    with pytest.raises(JobPaused):
        run_directory_overlap_analysis(database, config, None, job_id)
    assert _status(database, job_id) == "PAUSED"


def test_collection_analyser_honors_cancel(config, database, tmp_path):
    # A collections analyser that had no job_id before this change now honors cancellation.
    root = tmp_path / "src"
    root.mkdir()
    (root / "download0.bin").write_bytes(b"a")
    (root / "download1.bin").write_bytes(b"b")
    DriveScanner(database, config).scan(root, incremental=False)
    job_id = create_job(database, "DIRECTORY_SUMMARY")
    update_job(database, job_id, "RUNNING")
    request_cancel(database, job_id)
    with pytest.raises(JobCancelled):
        run_acquisition_batch_analysis(database, config, job_id=job_id)
    assert _status(database, job_id) == "CANCELLED"


def test_analysers_still_run_without_a_job(config, database, tmp_path):
    # The job_id path is optional: the same analysers complete normally when called without one.
    _two_overlapping_dirs(config, database, tmp_path)
    run_directory_overlap_analysis(database, config)  # no job_id
    result = run_acquisition_batch_analysis(database, config)  # no job_id
    assert isinstance(result, dict)


# --------------------------------------------------------------- pipeline-wide pause/cancel
# A multi-stage run (quickstart, analyse-all) is one pipeline job with a child job per stage.
# The buttons a user clicks land on whichever row they clicked, but the decision is about the
# run: requests escalate to the pipeline root, and workers poll their whole lineage.


def test_cancel_on_stage_escalates_to_pipeline_root(database):
    parent = create_job(database, "QUICKSTART")
    update_job(database, parent, "RUNNING")
    child = create_job(database, "SCAN", parent_job_id=parent)
    update_job(database, child, "RUNNING")
    request_cancel(database, child)  # the click lands on the stage row
    assert _status(database, parent) == "CANCELLING"
    _reset_poll_throttle()
    with pytest.raises(JobCancelled):
        checkpoint(database, child, processed_count=1)
    assert _status(database, child) == "CANCELLED"


def test_pause_on_pipeline_root_pauses_running_stage(database):
    parent = create_job(database, "QUICKSTART")
    update_job(database, parent, "RUNNING")
    child = create_job(database, "CONTENT_ANALYSIS", parent_job_id=parent)
    update_job(database, child, "RUNNING")
    request_pause(database, parent)
    _reset_poll_throttle()
    with pytest.raises(JobPaused):
        checkpoint(database, child, processed_count=1)
    assert _status(database, child) == "PAUSED"


def test_cancel_after_stage_finished_still_stops_the_run(database):
    # The race the per-stage buttons always lost: the stage completed before the click landed, so
    # the request was a no-op and the pipeline moved on. Escalation makes the click stick to the
    # still-running root, and the next stage refuses to start.
    parent = create_job(database, "QUICKSTART")
    update_job(database, parent, "RUNNING")
    finished = create_job(database, "SCAN", parent_job_id=parent)
    update_job(database, finished, "COMPLETED")
    request_cancel(database, finished)
    assert _status(database, finished) == "COMPLETED"  # terminal stage stays untouched
    assert _status(database, parent) == "CANCELLING"
    _reset_poll_throttle()
    with pytest.raises(JobCancelled), tracked_job(database, "EXACT_DUPLICATES", parent_job_id=parent):
        raise AssertionError("a stage must not run inside a cancelled pipeline")


def test_stage_cancel_settles_stage_and_pipeline(database):
    # End-to-end through tracked_job: the stage lands CANCELLED at its checkpoint and the
    # exception propagates so the enclosing pipeline job settles CANCELLED too.
    with (
        pytest.raises(JobCancelled),
        tracked_job(database, "QUICKSTART") as parent,
        tracked_job(database, "SCAN", parent_job_id=parent) as child,
    ):
        request_cancel(database, child)
        _reset_poll_throttle()
        checkpoint(database, child, processed_count=1)
    assert _status(database, child) == "CANCELLED"
    assert _status(database, parent) == "CANCELLED"


def test_pause_request_on_stage_of_pausing_pipeline_is_rejected(database):
    # request_pause validates against the ROOT's state: once the run is already pausing, another
    # pause click (on any row of the run) is an error the UI rounds down to a no-op.
    parent = create_job(database, "QUICKSTART")
    update_job(database, parent, "RUNNING")
    child = create_job(database, "SCAN", parent_job_id=parent)
    update_job(database, child, "RUNNING")
    request_pause(database, child)
    assert _status(database, parent) == "PAUSING"
    with pytest.raises(ValueError):
        request_pause(database, child)


def test_reaper_keeps_pipeline_root_alive_while_a_stage_beats(database):
    # A pipeline root's own row is only touched at stage boundaries; a recently-updated stage
    # must count as the root's heartbeat so a long remote run is not reaped mid-stage.
    parent = create_job(database, "QUICKSTART")
    update_job(database, parent, "RUNNING")
    child = create_job(database, "SCAN", parent_job_id=parent)
    update_job(database, child, "RUNNING")
    database.connect().execute(
        "UPDATE jobs SET host='remote-host',updated_at='2000-01-01 00:00:00' WHERE id=?",
        (parent,),
    )
    database.connect().execute("UPDATE jobs SET host='remote-host' WHERE id=?", (child,))
    database.connect().commit()
    reaped = jobs_module.reconcile_stale_jobs(database)
    assert reaped == []
    assert _status(database, parent) == "RUNNING"
