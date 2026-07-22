import json
import os
import platform
import signal
from contextlib import contextmanager
from collections.abc import Iterator
from typing import Any
from .database import Database


class JobCancelled(RuntimeError):
    """Raised at cooperative cancellation points; callers must leave durable state."""


class JobPaused(RuntimeError):
    """Raised when a job reaches a safe checkpoint after a pause request."""


JOB_STATES = {
    "PENDING",
    "RUNNING",
    "PAUSING",
    "PAUSED",
    "CANCELLING",
    "CANCELLED",
    "COMPLETED",
    "COMPLETED_WITH_ERRORS",
    "FAILED",
    "INTERRUPTED",
}

# States a finished job can never leave. A control request against one of these is a no-op.
TERMINAL_STATES = {"CANCELLED", "COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "INTERRUPTED"}

# States that imply a live worker is (or should be) touching the row. If the worker's process is
# gone, a job left in one of these is orphaned and the reaper settles it — see reconcile_stale_jobs.
ACTIVE_STATES = {"RUNNING", "PAUSING", "CANCELLING"}


def create_job(
    database: Database,
    job_type: str,
    scope: dict[str, Any] | None = None,
    config_fingerprint: str = "",
    total_estimate: int | None = None,
    worker_count: int = 1,
    parent_job_id: int | None = None,
) -> int:
    cur = database.connect().execute(
        "INSERT INTO jobs(job_type,scope_json,configuration_fingerprint,status,total_estimate,worker_count,host,process_id,parent_job_id) VALUES(?,?,?,'PENDING',?,?,?,?,?)",
        (
            job_type,
            json.dumps(scope or {}, sort_keys=True),
            config_fingerprint,
            total_estimate,
            worker_count,
            platform.node(),
            os.getpid(),
            parent_job_id,
        ),
    )
    database.connect().commit()
    assert cur.lastrowid is not None
    return cur.lastrowid


def update_job(
    database: Database,
    job_id: int,
    status: str | None = None,
    processed_count: int | None = None,
    current_item: str | None = None,
    checkpoint: dict[str, Any] | None = None,
    success_count: int | None = None,
    skip_count: int | None = None,
    error_count: int | None = None,
    total_estimate: int | None = None,
) -> None:
    if status is not None and status not in JOB_STATES:
        raise ValueError(f"invalid job status: {status}")
    updates, values = ["updated_at=CURRENT_TIMESTAMP"], []
    for key, value in (
        ("status", status),
        ("processed_count", processed_count),
        ("current_item", current_item),
        ("success_count", success_count),
        ("skip_count", skip_count),
        ("error_count", error_count),
        ("total_estimate", total_estimate),
    ):
        if value is not None:
            updates.append(f"{key}=?")
            values.append(value)
    if checkpoint is not None:
        updates.append("checkpoint_json=?")
        values.append(json.dumps(checkpoint, sort_keys=True))
    if status in {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELLED", "INTERRUPTED"}:
        updates.append("completed_at=CURRENT_TIMESTAMP")
    if status == "RUNNING":
        updates.append("started_at=COALESCE(started_at,CURRENT_TIMESTAMP)")
    values.append(job_id)
    database.connect().execute(f"UPDATE jobs SET {','.join(updates)} WHERE id=?", tuple(values))
    database.connect().commit()


def request_cancel(database: Database, job_id: int) -> None:
    """Ask a worker to stop and leave the job in a durable, cancelled state.

    Cancellation, like pause, is expressed through the status column so a restarted worker sees
    the same request. The transition is validated so a stray click on an already-finished job is a
    harmless no-op rather than a row that gets stuck in ``CANCELLING`` forever:

    * a terminal job is left untouched (there is nothing to cancel);
    * a ``PAUSED`` job has no live worker to observe the request, so it is finalized to
      ``CANCELLED`` directly instead of waiting at ``CANCELLING`` for a worker that never runs;
    * an active job is asked to cancel and settles at its next cooperative checkpoint.
    """
    row = database.fetch_one("SELECT status FROM jobs WHERE id=?", (job_id,))
    if not row:
        raise ValueError("job not found")
    status = row["status"]
    if status in TERMINAL_STATES:
        return
    if status == "PAUSED":
        update_job(database, job_id, "CANCELLED")
        return
    update_job(database, job_id, "CANCELLING")


def request_pause(database: Database, job_id: int) -> None:
    """Ask a worker to stop at its next durable checkpoint.

    A pause is deliberately not an in-memory primitive: the status is the control
    plane, so a worker that is restarted will observe the same request.
    """
    row = database.fetch_one("SELECT status FROM jobs WHERE id=?", (job_id,))
    if not row or row["status"] not in {"PENDING", "RUNNING"}:
        raise ValueError("only pending or running jobs can be paused")
    update_job(database, job_id, "PAUSING")


def resume_job(database: Database, job_id: int) -> None:
    row = database.fetch_one("SELECT status FROM jobs WHERE id=?", (job_id,))
    if not row or row["status"] not in {"PAUSED", "CANCELLED", "FAILED"}:
        raise ValueError("job is not resumable")
    update_job(database, job_id, "PENDING")


def cancellation_requested(database: Database, job_id: int) -> bool:
    row = database.fetch_one("SELECT status FROM jobs WHERE id=?", (job_id,))
    return bool(row and row["status"] in {"CANCELLING", "CANCELLED"})


def pause_requested(database: Database, job_id: int) -> bool:
    row = database.fetch_one("SELECT status FROM jobs WHERE id=?", (job_id,))
    return bool(row and row["status"] in {"PAUSING", "PAUSED"})


def check_cancelled(database: Database, job_id: int) -> None:
    if pause_requested(database, job_id):
        update_job(database, job_id, "PAUSED")
        raise JobPaused(f"job {job_id} paused")
    if cancellation_requested(database, job_id):
        update_job(database, job_id, "CANCELLED")
        raise JobCancelled(f"job {job_id} cancelled")


def checkpoint(
    database: Database,
    job_id: int | None,
    processed_count: int | None = None,
    current_item: str | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    """One cooperative checkpoint for an analyser loop.

    Honors any pending pause/cancel request (raising ``JobPaused`` / ``JobCancelled`` so the
    tracked job settles into a resumable terminal state), then records progress telemetry. A no-op
    when ``job_id`` is ``None`` so every analyser remains directly callable outside a job. Resume is
    idempotent re-run rather than seek-to-offset, so the recorded ``state`` is progress telemetry,
    not a mandatory resume cursor.
    """
    if job_id is None:
        return
    check_cancelled(database, job_id)
    if processed_count is not None or current_item is not None or state is not None:
        update_job(
            database,
            job_id,
            "RUNNING",
            processed_count=processed_count,
            current_item=current_item,
            checkpoint=state,
        )


@contextmanager
def tracked_job(
    database: Database,
    job_type: str,
    scope: dict[str, Any] | None = None,
    config_fingerprint: str = "",
    worker_count: int = 1,
    parent_job_id: int | None = None,
    existing_job_id: int | None = None,
) -> Iterator[int]:
    """Durably track an operation and turn Ctrl-C into a recoverable cancellation request."""
    job_id = existing_job_id or create_job(
        database,
        job_type,
        scope,
        config_fingerprint,
        worker_count=worker_count,
        parent_job_id=parent_job_id,
    )
    update_job(database, job_id, "RUNNING")
    previous_handler: Any = None

    def interrupt_handler(_signum: int, _frame: Any) -> None:
        request_cancel(database, job_id)
        raise JobCancelled(f"job {job_id} interrupted")

    try:
        if signal.getsignal(signal.SIGINT) is not None and threading_main_thread():
            previous_handler = signal.signal(signal.SIGINT, interrupt_handler)
        yield job_id
        check_cancelled(database, job_id)
    except JobPaused:
        # The checkpoint and partial transactions are already durable; keep the
        # terminal state resumable instead of converting a pause into a failure.
        update_job(database, job_id, "PAUSED")
        raise
    except JobCancelled:
        update_job(database, job_id, "CANCELLED")
        raise
    except BaseException:
        update_job(database, job_id, "FAILED")
        raise
    else:
        row = database.fetch_one("SELECT error_count FROM jobs WHERE id=?", (job_id,))
        update_job(
            database, job_id, "COMPLETED_WITH_ERRORS" if row and row["error_count"] else "COMPLETED"
        )
    finally:
        if previous_handler is not None:
            signal.signal(signal.SIGINT, previous_handler)


def threading_main_thread() -> bool:
    # signal.signal is restricted to the main thread; isolate the import for lean workers.
    import threading

    return threading.current_thread() is threading.main_thread()


def _worker_process_alive(pid: int | None, host: str | None, this_host: str) -> bool | None:
    """Best-effort liveness of the process that owns a job.

    Returns ``True``/``False`` only when the answer is trustworthy — the job was recorded on this
    same host and we can probe the pid. For a job from another host (a shared database on a
    different machine) the pid is meaningless here, so we return ``None`` ("undecidable") and the
    caller falls back to the heartbeat timeout.
    """
    if not pid or not host or host != this_host:
        return None
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists but is owned by another user — still alive.
        return True
    except OSError:
        return None
    return True


def reconcile_stale_jobs(
    database: Database, heartbeat_timeout_seconds: float = 120.0
) -> list[tuple[int, str]]:
    """Settle jobs whose worker has died, so the UI never shows a phantom "running" operation.

    Cooperative pause/cancel only works while a live worker is polling its checkpoints. If that
    worker's process disappears (the dashboard is restarted, the machine reboots, the CLI run is
    ``kill -9``'d) the row is stranded in ``RUNNING``/``PAUSING``/``CANCELLING`` forever — exactly
    the "these were stopped long ago but the web UI still shows them active" symptom.

    This detects an orphan two independent ways and needs only one to fire:

    * **process liveness** — the job was recorded on this host and its pid is no longer running;
    * **heartbeat** — no checkpoint has touched ``updated_at`` within ``heartbeat_timeout_seconds``
      (covers a dead worker on another host sharing the database).

    An orphaned job becomes ``INTERRUPTED`` (a terminal, honest state: "stopped without
    finishing"). ``PENDING`` (queued, no worker yet) and ``PAUSED`` (deliberately parked at a
    durable checkpoint) are never reaped. Returns the ``(job_id, new_status)`` pairs it changed.
    """
    from .core.progress import seconds_since  # local import keeps jobs.py import-cycle free

    this_host = platform.node()
    rows = database.fetch_all(
        "SELECT id,status,host,process_id,updated_at FROM jobs WHERE status IN ('RUNNING','PAUSING','CANCELLING')"
    )
    reaped: list[tuple[int, str]] = []
    for row in rows:
        alive = _worker_process_alive(row["process_id"], row["host"], this_host)
        if alive is True:
            continue  # a live worker owns this job; leave it alone
        stale_heartbeat = seconds_since(row["updated_at"]) > heartbeat_timeout_seconds
        if alive is None and not stale_heartbeat:
            # Undecidable pid and a recent heartbeat — assume a healthy remote/near worker.
            continue
        update_job(
            database, int(row["id"]), "INTERRUPTED", current_item="worker no longer running"
        )
        reaped.append((int(row["id"]), "INTERRUPTED"))
    return reaped
