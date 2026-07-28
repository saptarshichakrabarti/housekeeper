"""Migration introspection and resumable backfill helpers."""

from dataclasses import dataclass

from .constants import SCHEMA_VERSION


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    estimated_work: str


MIGRATIONS = (
    Migration(2, "content_identity_and_platform_tables", "legacy verified hashes"),
    Migration(3, "projects_and_analysis_entities", "project roots"),
    Migration(4, "resumable_operations_and_materialized_summaries", "batched entry cursor"),
)


def migration_plan(database) -> dict:
    exists = database.fetch_one(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
    )
    row = (
        database.fetch_one("SELECT COALESCE(MAX(version),0) AS version FROM schema_migrations")
        if exists
        else None
    )
    current = int(row["version"]) if row else 0
    entry_row = database.fetch_one("SELECT COUNT(*) AS n FROM filesystem_entries") if exists else None
    entry_count = int(entry_row["n"]) if entry_row else 0
    return {
        "current_version": current,
        "target_version": SCHEMA_VERSION,
        "pending": [m.__dict__ for m in MIGRATIONS if m.version > current],
        "backup_recommended": current < SCHEMA_VERSION,
        "estimated_affected_entries": entry_count if current < SCHEMA_VERSION else 0,
        "estimated_temporary_bytes": database.path.stat().st_size if database.path.exists() and current < SCHEMA_VERSION else 0,
    }
