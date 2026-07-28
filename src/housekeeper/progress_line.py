"""Live CLI progress for long-running synchronous commands (``quickstart``, ``scan``).

Polls the same ``jobs`` rows the dashboard's ``/fragments/jobs`` renders, through the database's
own read-only connection (WAL makes this safe alongside the command's own writer), so the CLI and
GUI can never disagree about a job's progress.
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from typing import Any, Self

from .core.progress import eta_seconds, format_duration, seconds_since, throughput
from .database import Database

_JOB_QUERY = (
    "SELECT job_type,status,processed_count,total_estimate,current_item,started_at FROM jobs"
    " WHERE status IN ('RUNNING','PENDING') ORDER BY updated_at DESC LIMIT 1"
)


def format_status_line(row: Any, stage_ref: dict[str, Any] | None = None) -> str:
    """Render one job row as a single status line — determinate, indeterminate, or pipeline-prefixed."""
    prefix = ""
    if stage_ref and stage_ref.get("total"):
        prefix = f"Stage {stage_ref['stage']}/{stage_ref['total']} · "
    job_type = str(row["job_type"]).lower()
    processed = row["processed_count"] or 0
    total = row["total_estimate"]
    rate = throughput(processed, seconds_since(row["started_at"]))
    if total:
        pct = min(100, int(processed * 100 / total))
        eta = eta_seconds(processed, total, rate)
        eta_text = f"  ETA {format_duration(eta)}" if eta is not None else ""
        return f"{prefix}[{job_type}] {pct}%  {processed:,}/{total:,}  {rate:,.1f}/s{eta_text}"
    current = f" · {row['current_item']}" if row["current_item"] else ""
    elapsed = format_duration(seconds_since(row["started_at"]))
    return f"{prefix}[{job_type}] {processed:,} processed · {rate:,.1f}/s · {elapsed}{current}"


class ProgressReporter:
    """Context manager: repaints a live status line on stderr while the wrapped call runs.

    A no-op (spawns no thread) when ``quiet`` — the caller passes ``quiet=True`` for ``--quiet``
    and ``--json`` runs so stdout/machine output stays clean with no special-casing at the print
    site. ``stage_ref``, if given, is a small dict the caller mutates from its own pipeline-stage
    callback (``{"stage": int, "total": int}``) — the same data path ``quickstart`` uses, just
    process-local rather than a DB column.
    """

    def __init__(
        self, database_path: Path, quiet: bool, stage_ref: dict[str, Any] | None = None
    ) -> None:
        self._database_path = database_path
        self._quiet = quiet
        self._stage_ref = stage_ref
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._tty = sys.stderr.isatty()
        self._wrote_line = False

    def __enter__(self) -> Self:
        if not self._quiet:
            self._thread = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1)
        if self._wrote_line:
            print(file=sys.stderr)  # end the \r-repainted line cleanly

    def _loop(self) -> None:
        database = Database(self._database_path)
        interval = 0.25 if self._tty else 2.0
        last_emit = 0.0
        try:
            while not self._stop.wait(interval):
                with database.read_connection() as conn:
                    row = conn.execute(_JOB_QUERY).fetchone()
                if row is None:
                    continue
                line = format_status_line(row, self._stage_ref)
                now = time.monotonic()
                if self._tty:
                    print(f"\r\x1b[K{line}", end="", file=sys.stderr, flush=True)
                    self._wrote_line = True
                elif now - last_emit >= interval:
                    print(line, file=sys.stderr, flush=True)
                    last_emit = now
        finally:
            database.close()
