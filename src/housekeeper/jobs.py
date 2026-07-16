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
}


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
    ):
        if value is not None:
            updates.append(f"{key}=?")
            values.append(value)
    if checkpoint is not None:
        updates.append("checkpoint_json=?")
        values.append(json.dumps(checkpoint, sort_keys=True))
    if status in {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELLED"}:
        updates.append("completed_at=CURRENT_TIMESTAMP")
    if status == "RUNNING":
        updates.append("started_at=COALESCE(started_at,CURRENT_TIMESTAMP)")
    values.append(job_id)
    database.connect().execute(f"UPDATE jobs SET {','.join(updates)} WHERE id=?", tuple(values))
    database.connect().commit()


def request_cancel(database: Database, job_id: int) -> None:
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
