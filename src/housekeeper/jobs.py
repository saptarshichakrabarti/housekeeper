"""Durable job tracking, cooperative pause/cancel, and orphan reconciliation.

Control requests use a side-channel file when SQLite's single writer is busy, so a UI click
is never lost mid-transaction. Workers poll lineage (pipeline root + stage) at checkpoints.
"""

import json
import os
import platform
import signal
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
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

# How long a control request waits for SQLite's single write lock before falling back to the file
# channel below. Short on purpose: the click must feel immediate, and the file has already recorded
# the request by then, so losing the race costs nothing.
CONTROL_BUSY_TIMEOUT_MS = 200


def control_path(database: Database, job_id: int) -> Path:
    return database.path.parent / f"job-{job_id}.stop"


def pending_control(database: Database, job_id: int) -> str:
    """Out-of-band stop request for this job: ``CANCELLING``, ``PAUSING``, or ``""``.

    Written to a file first because SQLite has one writer — a mid-stage transaction used to
    block the UI write past ``busy_timeout`` and drop the click. The worker owns the lock and
    applies the durable transition at its next checkpoint.
    """
    try:
        return control_path(database, job_id).read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _request_control(database: Database, job_id: int, status: str) -> None:
    """Record a stop request out-of-band, then try to publish it to the row as well."""
    if status == "PAUSING" and pending_control(database, job_id) == "CANCELLING":
        return  # cancel wins over pause here as it does at the checkpoint
    control_path(database, job_id).write_text(status, encoding="utf-8")
    connection = database.connect()
    connection.execute(f"PRAGMA busy_timeout={CONTROL_BUSY_TIMEOUT_MS}")
    try:
        # Guarded in SQL rather than by the status read above: the worker can see the file and
        # settle the row while this statement is still waiting for the lock, and an unguarded write
        # would then resurrect the finished job as CANCELLING, with no worker left to settle it.
        terminal = ",".join("?" for _ in TERMINAL_STATES)
        changed = connection.execute(
            f"UPDATE jobs SET status=?,updated_at=CURRENT_TIMESTAMP "
            f"WHERE id=? AND status NOT IN ({terminal})",
            (status, job_id, *sorted(TERMINAL_STATES)),
        ).rowcount
        connection.commit()
        if not changed:
            _clear_control(database, job_id)  # already finished; there is nothing to ask of it
    except sqlite3.OperationalError:
        # A worker is mid-transaction. It will see the file and settle the row itself; the UI reads
        # the same file, so the job still shows as stopping in the meantime.
        #
        # Rolled back, not merely swallowed: Python emitted an implicit BEGIN before that UPDATE, so
        # without this the dashboard's shared connection stays in an open transaction and every later
        # read on it serves a pinned snapshot — a resume would then re-read the pre-pause status and
        # refuse the job as unresumable.
        connection.rollback()
    finally:
        connection.execute("PRAGMA busy_timeout=5000")


def _clear_control(database: Database, job_id: int) -> None:
    control_path(database, job_id).unlink(missing_ok=True)


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
    # `database purge` deletes every job row, so ids start again at 1. Drop any stop request left
    # over from the job that used to have this id, or the new one would cancel itself immediately.
    _clear_control(database, cur.lastrowid)
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
    # A state transition is control-plane information another process acts on — a dashboard
    # showing the job, the reaper deciding it is orphaned — so it is committed at once. A pure
    # progress update is not: it rides the enclosing batch and becomes visible at the next commit,
    # which is what turned a million-entry stage into a million fsync-bounded transactions.
    if status is not None:
        database.connect().commit()
    if status in TERMINAL_STATES or status == "PAUSED":
        # PAUSED is honoured-but-not-terminal, and its request must still be cleared: the file
        # outlived the pause, so resuming the same row (``resume_job``) re-paused it at the first
        # checkpoint. Only this job's own request is dropped — a pipeline root keeps its file until
        # it settles too, so stages still in flight go on observing the pause.
        _clear_control(database, job_id)  # the request has been honoured


# Parent chains are shallow (pipeline -> stage today); the bound only guards a corrupt cycle.
_MAX_LINEAGE_DEPTH = 16


def pipeline_root(database: Database, job_id: int) -> dict[str, Any] | None:
    """Topmost job of the pipeline ``job_id`` belongs to (itself if standalone).

    Pause/cancel targets the run, not the current stage — otherwise the pipeline continues
    or the click lands on an already-finished stage and does nothing.
    """
    current = job_id
    row = None
    for _ in range(_MAX_LINEAGE_DEPTH):
        row = database.fetch_one(
            "SELECT id,status,parent_job_id FROM jobs WHERE id=?", (current,)
        )
        if not row:
            return None
        parent = row["parent_job_id"]
        if parent is None or int(parent) == int(row["id"]):
            break
        current = int(parent)
    return {"id": int(row["id"]), "status": row["status"]} if row else None


def request_cancel(database: Database, job_id: int) -> None:
    """Ask the pipeline root's worker to stop; leave a durable cancelled state.

    Escalates to the root so one click cancels the whole run. Status is the control plane
    (survives restart). Terminal jobs are no-ops; ``PAUSED`` (no live worker) is finalized to
    ``CANCELLED`` directly; active jobs settle at the next checkpoint.
    """
    root = pipeline_root(database, job_id)
    if not root:
        raise ValueError("job not found")
    status = root["status"]
    if status in TERMINAL_STATES:
        return
    if status == "PAUSED":
        update_job(database, root["id"], "CANCELLED")
        return
    _request_control(database, root["id"], "CANCELLING")


def request_pause(database: Database, job_id: int) -> None:
    """Ask the pipeline root's worker to stop at its next durable checkpoint.

    Escalates to the root so the whole run parks. Status (not an in-memory flag) is the
    control plane so a restarted worker sees the same request.
    """
    root = pipeline_root(database, job_id)
    if not root or root["status"] not in {"PENDING", "RUNNING"}:
        raise ValueError("only pending or running jobs can be paused")
    _request_control(database, root["id"], "PAUSING")


def resume_job(database: Database, job_id: int) -> None:
    row = database.fetch_one("SELECT status FROM jobs WHERE id=?", (job_id,))
    if not row or row["status"] not in {"PAUSED", "CANCELLED", "FAILED"}:
        raise ValueError("job is not resumable")
    update_job(database, job_id, "PENDING")


# One query for the statuses of a job and every ancestor pipeline it belongs to. Bounded so a
# corrupt parent cycle degrades to a truncated (but still correct) answer instead of a hang.
_LINEAGE_STATUS_SQL = f"""
WITH RECURSIVE lineage(id, parent_job_id, status, depth) AS (
    SELECT id, parent_job_id, status, 0 FROM jobs WHERE id=?
    UNION ALL
    SELECT j.id, j.parent_job_id, j.status, l.depth + 1
    FROM jobs j JOIN lineage l ON j.id = l.parent_job_id
    WHERE l.depth < {_MAX_LINEAGE_DEPTH}
)
SELECT id, status FROM lineage
"""


def _lineage_statuses(database: Database, job_id: int) -> set[str]:
    """Statuses for this job and its ancestors, including out-of-band file requests.

    A request that missed the write lock lives only in a file (see ``pending_control``); one
    poll must answer both channels.
    """
    statuses = set()
    for row in database.fetch_all(_LINEAGE_STATUS_SQL, (job_id,)):
        statuses.add(str(row["status"]))
        statuses.add(pending_control(database, int(row["id"])))
    return statuses - {""}


def stop_requested(database: Database, job_id: int) -> str:
    """Stop this job is under (own or ancestor): ``CANCELLING``, ``PAUSING``, or ``""``.

    Requests escalate to the pipeline root, so lineage — not the stage row alone — drives the
    UI. Cancel wins over pause, as at the checkpoint.
    """
    statuses = _lineage_statuses(database, job_id)
    if statuses & {"CANCELLING", "CANCELLED"}:
        return "CANCELLING"
    if statuses & {"PAUSING", "PAUSED"}:
        return "PAUSING"
    return ""


def cancellation_requested(database: Database, job_id: int) -> bool:
    return bool(_lineage_statuses(database, job_id) & {"CANCELLING", "CANCELLED"})


def pause_requested(database: Database, job_id: int) -> bool:
    return bool(_lineage_statuses(database, job_id) & {"PAUSING", "PAUSED"})


# Cancellation is a human-scale event: polling faster than this buys nothing and costs a query.
CANCELLATION_POLL_SECONDS = 0.25
# Last poll time per job. Pipelines interleave checks between a stage's per-entry checkpoints and
# tracked_job's own entry/exit checks; a single slot would flip on every call and defeat the
# throttle. Keyed by (database, job id): job ids restart at 1 per workspace.
# Plain dict, oldest-first eviction at 8 — a pipeline touches ~2 jobs at once.
_POLL_CACHE_SIZE = 8
_last_poll: dict[tuple[str, int], float] = {}

# How often a progress-only update is made visible to other processes. Progress is telemetry, not
# state: publishing it costs a transaction, and nobody reads a bar faster than this.
PROGRESS_COMMIT_SECONDS = 0.25
_last_progress_commit = 0.0


def check_cancelled(database: Database, job_id: int) -> None:
    """Honour a pending pause/cancel. Called per entry, so nearly free.

    One throttled lineage SELECT (was up to four statements per entry). A stage stops when the
    *pipeline* is asked to pause/cancel. Cancel wins over pause.
    """
    key = (str(database.path), job_id)
    now = time.monotonic()
    if now - _last_poll.get(key, 0.0) < CANCELLATION_POLL_SECONDS:
        return
    if len(_last_poll) >= _POLL_CACHE_SIZE and key not in _last_poll:
        del _last_poll[next(iter(_last_poll))]
    _last_poll[key] = now
    statuses = _lineage_statuses(database, job_id)
    if statuses & {"CANCELLING", "CANCELLED"}:
        update_job(database, job_id, "CANCELLED")
        raise JobCancelled(f"job {job_id} cancelled")
    if statuses & {"PAUSING", "PAUSED"}:
        update_job(database, job_id, "PAUSED")
        raise JobPaused(f"job {job_id} paused")


def checkpoint(
    database: Database,
    job_id: int | None,
    processed_count: int | None = None,
    current_item: str | None = None,
    state: dict[str, Any] | None = None,
) -> None:
    """Cooperative analyser checkpoint: honour stop requests, then record progress.

    No-op when ``job_id`` is ``None`` (analysers stay callable outside a job). Resume is
    idempotent re-run, so ``state`` is telemetry, not a seek cursor.
    """
    global _last_progress_commit
    if job_id is None:
        return
    check_cancelled(database, job_id)
    if processed_count is None and current_item is None and state is None:
        return
    # No status: this is progress, not a transition, so the UPDATE does not commit on its own.
    update_job(
        database,
        job_id,
        processed_count=processed_count,
        current_item=current_item,
        checkpoint=state,
    )
    # Publish it at a human-visible cadence rather than per row. Some analysers checkpoint once per
    # candidate pair, which at inventory scale is hundreds of thousands of transactions to move a
    # progress bar nobody can read that fast.
    now = time.monotonic()
    if now - _last_progress_commit >= PROGRESS_COMMIT_SECONDS:
        _last_progress_commit = now
        database.connect().commit()


def _settle_stage_wal(database: Database) -> None:
    """At a stage boundary: record the WAL's peak size and checkpoint what it can, best-effort.

    A PASSIVE checkpoint never waits for a reader, so it is safe to run after every stage — it keeps
    the WAL left by one analyser from being carried, and grown, by the next. Wrapped so a settle can
    never convert a real stage failure into a checkpoint error when it runs from a ``finally``.
    """
    from .core import counters

    try:
        if counters.is_recording():
            counters.record_max("wal_bytes_stage_end", database.wal_bytes())
        database.checkpoint_wal("PASSIVE")
    except Exception:  # noqa: BLE001,S110 - a best-effort settle must never mask the stage's own outcome
        pass


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
    """Durably track an operation; turn Ctrl-C into a recoverable cancellation request.

    Stage = transaction boundary: commit on success, rollback on failure. Graph-cache
    invalidation for relationship writers runs once here, not per relationship.
    """
    from .relationships import invalidate_graph_cache

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
        # A stage that starts inside a pipeline that was just paused/cancelled must not run at
        # all: honour the inherited request before doing any work. Without this, a control click
        # that lands on a stage boundary would be a no-op and the run would simply move on.
        check_cancelled(database, job_id)
        yield job_id
        database.connect().commit()
        invalidate_graph_cache(database)
        check_cancelled(database, job_id)
    except JobPaused:
        # A pause stops at a checkpoint, so the work up to it is real: commit it and keep the
        # terminal state resumable instead of converting a pause into a failure.
        database.connect().commit()
        update_job(database, job_id, "PAUSED")
        raise
    except JobCancelled:
        # Likewise cancellation — it is cooperative and lands on a checkpoint, so the completed
        # portion stays durable. Resume is idempotent re-run, never seek-to-offset.
        database.connect().commit()
        update_job(database, job_id, "CANCELLED")
        raise
    except BaseException:
        # The stage owns the transaction, so a failed stage leaves nothing half-written. The job
        # row itself is written afterwards, on its own, so the failure is still recorded.
        database.connect().rollback()
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
        # Whatever the outcome — committed, paused, cancelled, rolled back — this is a stage
        # boundary, so settle the WAL here rather than letting it accumulate across the pipeline.
        _settle_stage_wal(database)


def threading_main_thread() -> bool:
    # signal.signal is restricted to the main thread; isolate the import for lean workers.
    import threading

    return threading.current_thread() is threading.main_thread()


def _worker_process_alive(pid: int | None, host: str | None, this_host: str) -> bool | None:
    """Best-effort liveness of the job's owning process.

    ``True``/``False`` only when pid was recorded on this host; otherwise ``None`` so the
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
    """Settle jobs whose worker has died so the UI never shows a phantom active operation.

    Orphans are detected by process liveness (same-host pid) or heartbeat timeout (covers a
    dead worker on another host). Settled to ``INTERRUPTED``. ``PENDING`` and ``PAUSED`` are
    never reaped. Returns ``(job_id, new_status)`` pairs changed.
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
        # A pipeline root's own row is only touched at stage boundaries, so its heartbeat is the
        # freshest of itself and its stage jobs — a busy stage keeps the whole pipeline alive.
        child = database.fetch_one(
            "SELECT MAX(updated_at) latest FROM jobs WHERE parent_job_id=?", (row["id"],)
        )
        heartbeats = [row["updated_at"]] + ([child["latest"]] if child and child["latest"] else [])
        stale_heartbeat = min(seconds_since(value) for value in heartbeats) > heartbeat_timeout_seconds
        if alive is None and not stale_heartbeat:
            # Undecidable pid and a recent heartbeat — assume a healthy remote/near worker.
            continue
        update_job(
            database, int(row["id"]), "INTERRUPTED", current_item="worker no longer running"
        )
        reaped.append((int(row["id"]), "INTERRUPTED"))
    return reaped
