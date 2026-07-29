from pathlib import Path

from .config import AppConfig
from .database import Database
from .path_utils import normalize_absolute_path


def backup(database: Database, output: Path) -> Path:
    return database.backup(output)


def integrity_check(database: Database) -> str:
    return database.integrity_check()


def optimize(database: Database) -> None:
    database.connect().execute("PRAGMA optimize")
    database.connect().commit()


def checkpoint(database: Database, mode: str = "PASSIVE") -> tuple[int, int, int]:
    return database.checkpoint_wal(mode)


def vacuum(database: Database) -> None:
    database.vacuum()


def purge_runs(
    database: Database, config: AppConfig, keep_job_id: int | None = None
) -> dict[str, object]:
    """Delete every recorded run, everything derived from one, and the reports they generated.

    The source drive is never touched: only the workspace's own database rows and report files go.
    ``keep_job_id`` spares the job row tracking this purge — see ``Database.purge_runs``.
    """
    deleted = database.purge_runs(keep_job_id)
    reports = normalize_absolute_path(config.workspace / config.data["workspace"]["reports_dir"])
    workspace = normalize_absolute_path(config.workspace)
    # A misconfigured reports_dir would otherwise aim a recursive delete anywhere on the filesystem.
    if not reports.is_relative_to(workspace) or reports == workspace:
        raise ValueError(f"reports_dir must be a directory inside the workspace: {reports}")
    removed = 0
    if reports.is_dir():
        for path in sorted(reports.rglob("*"), reverse=True):  # children sort after their parent
            path.rmdir() if path.is_dir() else path.unlink()
            removed += 1
    if keep_job_id is None:
        # Nobody else is recording this purge (the dashboard runner wraps it in a tracked job and
        # passes keep_job_id). Leave the marker afterwards, so the next scan's "what changed" digest
        # can say the history was purged instead of reporting a drive full of new files.
        from .jobs import create_job, update_job

        job = create_job(database, "PURGE", {"rows_deleted": sum(deleted.values())})
        update_job(database, job, "COMPLETED")
    return {"rows_deleted": deleted, "report_paths_removed": removed}
