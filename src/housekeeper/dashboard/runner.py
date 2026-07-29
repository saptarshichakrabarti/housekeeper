"""Background single-worker runner for GUI-triggered operations.

At most one operation (scan/analyse/classify/report) runs at a time per workspace. The worker
thread opens its own database connection — SQLite connections must never be shared across
threads, and WAL mode makes that one writer safe alongside the dashboard's own reader and any
concurrent CLI reader.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..config import AppConfig, config_fingerprint
from ..database import Database
from ..jobs import JobCancelled, JobPaused, tracked_job

# The GUI's analyse control is a curated, common-path subset of the CLI's full ~25-kind matrix
# (scope filters like --under/--mime stay CLI-only). "all" runs every kind below in sequence.
analyse_KINDS = (
    "exact-duplicates",
    "directory-overlap",
    "documents",
    "images",
    "media",
    "archives",
    "projects",
)
REPORT_KINDS = (
    "summary",
    "inventory",
    "exact-duplicates",
    "directory-overlap",
    "document-versions",
    "images",
    "projects",
    "errors",
)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def _idle_status() -> dict[str, Any]:
    return {
        "state": "idle",
        "operation": None,
        "stage": None,
        "stage_total": None,
        "current_item": None,
        "started_at": None,
        "error": None,
    }


class OperationRunner:
    """Serializes GUI-triggered pipeline operations onto a single background worker thread."""

    def __init__(self, config: AppConfig) -> None:
        self._config = config
        self._executor = ThreadPoolExecutor(max_workers=1)
        self._lock = threading.Lock()
        self._status: dict[str, Any] = _idle_status()

    def submit(self, operation: str, **kwargs: Any) -> str:
        """Accept and start ``operation`` in the background, or return "busy" if one is running."""
        with self._lock:
            if self._status["state"] == "running":
                return "busy"
            self._status = {
                **_idle_status(),
                "state": "running",
                "operation": operation,
                "started_at": _now_iso(),
            }
        self._executor.submit(self._run, operation, kwargs)
        return "accepted"

    def status(self) -> dict[str, Any]:
        with self._lock:
            return dict(self._status)

    def _on_progress(self, message: str, stage: int, stage_total: int) -> None:
        with self._lock:
            self._status["stage"] = stage
            self._status["stage_total"] = stage_total
            self._status["current_item"] = message

    def _run(self, operation: str, kwargs: dict[str, Any]) -> None:
        database = Database(self._config.database_path)
        try:
            database.initialize()
            if operation == "quickstart":
                from ..quickstart import run_quickstart

                run_quickstart(
                    database, self._config, Path(kwargs["source"]), progress=self._on_progress
                )
            elif operation == "analyse":
                self._run_analyse(database, kwargs["kind"])
            elif operation == "classify":
                self._run_classify(database)
            elif operation == "report":
                self._run_report(database, kwargs["kind"])
            elif operation == "purge":
                from ..database_maintenance import purge_runs

                purge_runs(database, self._config)
            else:
                raise ValueError(f"unknown operation: {operation}")
            # analyse/classify change the counts and charts the overview shows, so refresh the
            # materialized summaries here too (scan already did its own), then settle the WAL and
            # planner stats so the next dashboard load stays fast on a large inventory.
            database.refresh_materialized_summaries()
            database.optimize_after_write()
            with self._lock:
                self._status["state"] = "idle"
        except (JobCancelled, JobPaused) as exc:
            # A deliberate stop is not a failure. Surface it as its own state so the Run page shows
            # "cancelled"/"paused" instead of a red error card for something the user asked for.
            with self._lock:
                self._status["state"] = (
                    "cancelled" if isinstance(exc, JobCancelled) else "paused"
                )
        except Exception as exc:  # noqa: BLE001 - surfaced to the GUI; the worker must never crash
            with self._lock:
                self._status["state"] = "error"
                self._status["error"] = str(exc)
        finally:
            database.close()

    def _tracked(
        self,
        database: Database,
        job_type: str,
        label: str,
        callback: Callable[[int], object],
        parent_job_id: int | None = None,
    ) -> object:
        with tracked_job(
            database,
            job_type,
            {"gui": label},
            config_fingerprint(self._config),
            parent_job_id=parent_job_id,
        ) as job_id:
            return callback(job_id)

    def _run_analyse(self, database: Database, kind: str) -> None:
        from ..analysers.directory_overlap import run_directory_overlap_analysis
        from ..analysers.exact_duplicates import run_exact_duplicate_analysis
        from ..analysers.projects import run_project_analysis
        from ..analysers.registry import run_content_analysis
        from ..analysers.scope import AnalyserScope

        # Standalone analyse would otherwise run over all scan history; restrict it to the current
        # inventory so re-scanning the same drive never makes a unique file look duplicated.
        inventory = AnalyserScope.current(database)

        def content_analysis_step(name: str) -> Callable[[int], object]:
            # Named factory (rather than a lambda default-arg trick) so each closure captures its
            # own ``name`` instead of all sharing the loop variable's final value.
            # ``inventory`` was built two lines up and handed to every other stage; content
            # analysis was the one that did not get it, and so ran over all scan history.
            return lambda job: run_content_analysis(
                database, self._config, name, inventory, job_id=job
            )

        dispatch: dict[str, tuple[str, Callable[[int], object]]] = {
            "exact-duplicates": (
                "EXACT_DUPLICATES",
                lambda job: run_exact_duplicate_analysis(
                    database, self._config, job_id=job, scope=inventory
                ),
            ),
            "directory-overlap": (
                "DIRECTORY_OVERLAP",
                lambda job: run_directory_overlap_analysis(
                    database, self._config, inventory, job_id=job
                ),
            ),
            "projects": (
                "PROJECT_ANALYSIS",
                lambda job: run_project_analysis(database, self._config, job_id=job),
            ),
        }
        for name in ("documents", "images", "media", "archives"):
            dispatch[name] = ("CONTENT_ANALYSIS", content_analysis_step(name))
        if kind != "all" and kind not in dispatch:
            raise ValueError(f"unknown analyse kind: {kind}")
        if kind == "all":
            # One pipeline job spanning every kind, so a single pause/cancel stops the whole
            # sequence instead of only the analyser that happened to be running when clicked.
            with tracked_job(
                database, "ANALYSE_ALL", {"gui": "analyse-all"}, config_fingerprint(self._config)
            ) as pipeline_job:
                for name in analyse_KINDS:
                    job_type, callback = dispatch[name]
                    self._tracked(database, job_type, name, callback, parent_job_id=pipeline_job)
        else:
            job_type, callback = dispatch[kind]
            self._tracked(database, job_type, kind, callback)

    def _run_classify(self, database: Database) -> None:
        from ..policies import classify_all_entries

        self._tracked(
            database,
            "CLASSIFICATION",
            "classify",
            lambda job: classify_all_entries(database, self._config, job_id=job),
        )

    def _run_report(self, database: Database, kind: str) -> None:
        from ..reports.generator import generate_all_reports, generate_report

        if kind == "all":
            self._tracked(
                database,
                "REPORT_GENERATION",
                "reports",
                lambda job: generate_all_reports(database, self._config, job_id=job),
            )
        elif kind in REPORT_KINDS:
            self._tracked(
                database,
                "REPORT_GENERATION",
                kind,
                lambda job: generate_report(kind, database, self._config),
            )
        else:
            raise ValueError(f"unknown report kind: {kind}")
