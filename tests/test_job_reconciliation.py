"""Orphaned-job reconciliation and honest control transitions.

Cooperative pause/cancel only settles a job while its worker is alive to observe the request. When
the worker's process dies (a killed CLI run, a restarted dashboard) the row is stranded in an
active state forever — the "stopped long ago but the web UI still shows it running" symptom. The
reaper (:func:`reconcile_stale_jobs`) closes that gap, and :func:`request_cancel` is now
transition-safe so a stray click can never manufacture a new stuck state.
"""

import os
import platform

import pytest

from housekeeper.jobs import (
    create_job,
    reconcile_stale_jobs,
    request_cancel,
    request_pause,
    update_job,
)


def _status(database, job_id):
    return database.fetch_one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"]


def _set_host_pid(database, job_id, host, pid):
    database.connect().execute(
        "UPDATE jobs SET host=?,process_id=? WHERE id=?", (host, pid, job_id)
    )
    database.connect().commit()


def _dead_pid() -> int:
    """A pid that is not running: fork a child, reap it, reuse its (now-free) pid."""
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child exits immediately
        os._exit(0)
    os.waitpid(pid, 0)
    return pid


def test_reaper_interrupts_running_job_from_dead_local_process(database):
    job_id = create_job(database, "SCAN")
    update_job(database, job_id, "RUNNING", processed_count=42)
    _set_host_pid(database, job_id, platform.node(), _dead_pid())

    reaped = reconcile_stale_jobs(database)

    assert reaped == [(job_id, "INTERRUPTED")]
    assert _status(database, job_id) == "INTERRUPTED"
    # Progress the worker got through stays on the row; a terminal timestamp is stamped.
    row = database.fetch_one(
        "SELECT processed_count,completed_at FROM jobs WHERE id=?", (job_id,)
    )
    assert row["processed_count"] == 42
    assert row["completed_at"] is not None


def test_reaper_settles_a_stuck_pausing_job(database):
    """The screenshot case: a job requested to pause whose worker vanished mid-request."""
    job_id = create_job(database, "SCAN")
    update_job(database, job_id, "RUNNING")
    request_pause(database, job_id)
    assert _status(database, job_id) == "PAUSING"
    _set_host_pid(database, job_id, platform.node(), _dead_pid())

    reconcile_stale_jobs(database)

    assert _status(database, job_id) == "INTERRUPTED"


def test_reaper_leaves_a_live_local_job_alone(database):
    # This very test process is alive, so a job pinned to our own pid must never be reaped.
    job_id = create_job(database, "SCAN")
    update_job(database, job_id, "RUNNING")
    _set_host_pid(database, job_id, platform.node(), os.getpid())

    assert reconcile_stale_jobs(database) == []
    assert _status(database, job_id) == "RUNNING"


def test_reaper_never_touches_pending_paused_or_terminal_jobs(database):
    pending = create_job(database, "SCAN")  # stays PENDING
    paused = create_job(database, "SCAN")
    update_job(database, paused, "RUNNING")
    request_pause(database, paused)
    update_job(database, paused, "PAUSED")
    done = create_job(database, "SCAN")
    update_job(database, done, "COMPLETED")

    assert reconcile_stale_jobs(database) == []
    assert _status(database, pending) == "PENDING"
    assert _status(database, paused) == "PAUSED"
    assert _status(database, done) == "COMPLETED"


def test_reaper_uses_heartbeat_for_remote_jobs(database):
    # A job from another host cannot be pid-probed; a fresh heartbeat means "assume healthy".
    job_id = create_job(database, "SCAN")
    update_job(database, job_id, "RUNNING")
    _set_host_pid(database, job_id, "some-other-host", 999999)
    assert reconcile_stale_jobs(database, heartbeat_timeout_seconds=3600) == []
    assert _status(database, job_id) == "RUNNING"

    # A stale heartbeat (updated_at far in the past) reaps it even without a pid probe.
    database.connect().execute(
        "UPDATE jobs SET updated_at='2000-01-01 00:00:00' WHERE id=?", (job_id,)
    )
    database.connect().commit()
    assert reconcile_stale_jobs(database, heartbeat_timeout_seconds=120) == [
        (job_id, "INTERRUPTED")
    ]


def test_request_cancel_on_terminal_job_is_a_noop(database):
    job_id = create_job(database, "SCAN")
    update_job(database, job_id, "COMPLETED")
    request_cancel(database, job_id)  # must not resurrect it into CANCELLING
    assert _status(database, job_id) == "COMPLETED"


def test_request_cancel_finalizes_a_paused_job_directly(database):
    # A paused job has no live worker to observe a cancel, so it must go straight to CANCELLED
    # rather than parking at CANCELLING waiting for a worker that will never run again.
    job_id = create_job(database, "SCAN")
    update_job(database, job_id, "RUNNING")
    request_pause(database, job_id)
    update_job(database, job_id, "PAUSED")

    request_cancel(database, job_id)

    assert _status(database, job_id) == "CANCELLED"


def test_request_cancel_on_running_job_requests_cancelling(database):
    job_id = create_job(database, "SCAN")
    update_job(database, job_id, "RUNNING")
    request_cancel(database, job_id)
    assert _status(database, job_id) == "CANCELLING"


def test_request_cancel_missing_job_raises(database):
    with pytest.raises(ValueError):
        request_cancel(database, 999999)
