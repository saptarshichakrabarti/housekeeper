"""Source-code / research project detection and regenerable-content accounting.

Detects project roots from marker files, then attributes each project's recursive bytes to
source, generated (regenerable), and environment buckets so the review workflow can surface
high-confidence regenerable directories without ever moving a directory wholesale.  ``.git``
is always treated as protected project state, never as clutter.
"""

from __future__ import annotations

import json

PROJECT_MARKERS = {
    ".git",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "environment.yml",
    "environment.yaml",
    "Pipfile",
    "Pipfile.lock",
    "poetry.lock",
    "uv.lock",
    "package.json",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.toml",
    "Cargo.lock",
    "go.mod",
    "pom.xml",
    "build.gradle",
    "Makefile",
    "CMakeLists.txt",
    "Dockerfile",
    "README",
    "README.md",
}

DEPENDENCY_SPECS = {
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "environment.yml",
    "environment.yaml",
    "Pipfile",
    "package.json",
    "Cargo.toml",
    "go.mod",
}

LOCKFILES = {
    "Pipfile.lock",
    "poetry.lock",
    "uv.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "go.sum",
}

GENERATED_DIRS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".ipynb_checkpoints",
    "dist",
    "build",
    "target",
    "coverage",
    "htmlcov",
}

ENVIRONMENT_DIRS = {"node_modules", ".venv", "venv", "env"}


def classify_project_kind(markers: list[str]) -> str:
    marker_set = set(markers)
    if marker_set & {"pyproject.toml", "requirements.txt", "Pipfile", "environment.yml"}:
        return "python"
    if marker_set & {"package.json"}:
        return "javascript"
    if "Cargo.toml" in marker_set:
        return "rust"
    if "go.mod" in marker_set:
        return "go"
    if marker_set & {"pom.xml", "build.gradle"}:
        return "java"
    return "source"


def detect_reproducibility_signals(markers: list[str]) -> dict[str, bool]:
    marker_set = set(markers)
    return {
        "has_dependency_spec": bool(marker_set & DEPENDENCY_SPECS),
        "has_lockfile": bool(marker_set & LOCKFILES),
        "has_git": ".git" in marker_set,
        "has_dockerfile": "Dockerfile" in marker_set,
    }


def calculate_project_storage_breakdown(
    database, scan_run_id: int, root_relative_path: str
) -> dict[str, int]:
    """Split a project's recursive file bytes into source / generated / environment buckets."""
    prefix = f"{root_relative_path}/" if root_relative_path else ""
    like = f"{prefix}%"
    breakdown = {"source": 0, "generated": 0, "environment": 0, "file_count": 0}
    for row in database.iter_rows(
        "SELECT relative_path,size_bytes FROM filesystem_entries WHERE scan_run_id=? AND entry_type='file' AND relative_path LIKE ?",
        (scan_run_id, like),
    ):
        rel = str(row["relative_path"])[len(prefix) :]
        segments = set(rel.replace("\\", "/").split("/")[:-1])
        size = int(row["size_bytes"] or 0)
        breakdown["file_count"] += 1
        if segments & ENVIRONMENT_DIRS:
            breakdown["environment"] += size
        elif segments & GENERATED_DIRS:
            breakdown["generated"] += size
        else:
            breakdown["source"] += size
    return breakdown


def run_project_analysis(database, config, scope=None, job_id: int | None = None):
    from ..jobs import check_cancelled, update_job
    from ..relationships import upsert_relationship

    dirs = database.fetch_all(
        "SELECT id,name,relative_path,scan_run_id FROM filesystem_entries WHERE entry_type='directory'"
    )
    if scope:
        from .scope import scoped_entry_ids

        allowed = scoped_entry_ids(database, scope, "directory")
        dirs = [directory for directory in dirs if int(directory["id"]) in allowed]

    for index, directory in enumerate(dirs, start=1):
        if job_id:
            check_cancelled(database, job_id)
        names = {
            r["name"]
            for r in database.fetch_all(
                "SELECT name FROM filesystem_entries WHERE scan_run_id=? AND parent_entry_id=?",
                (directory["scan_run_id"], directory["id"]),
            )
        }
        found = sorted(names & PROJECT_MARKERS)
        # A lone README is not a project; require a real build/dependency/VCS marker.
        if not found or found == ["README"] or found == ["README.md"]:
            continue
        kind = classify_project_kind(found)
        signals = detect_reproducibility_signals(found)
        breakdown = calculate_project_storage_breakdown(
            database, int(directory["scan_run_id"]), str(directory["relative_path"])
        )
        database.connect().execute(
            """INSERT INTO projects(root_entry_id,name,kind,markers_json,source_size_bytes,generated_size_bytes,environment_size_bytes,git_status)
               VALUES(?,?,?,?,?,?,?,?)
               ON CONFLICT(root_entry_id) DO UPDATE SET name=excluded.name,kind=excluded.kind,markers_json=excluded.markers_json,
               source_size_bytes=excluded.source_size_bytes,generated_size_bytes=excluded.generated_size_bytes,
               environment_size_bytes=excluded.environment_size_bytes,git_status=excluded.git_status,updated_at=CURRENT_TIMESTAMP""",
            (
                directory["id"],
                directory["name"],
                kind,
                json.dumps(found),
                breakdown["source"],
                breakdown["generated"],
                breakdown["environment"],
                "present" if signals["has_git"] else "absent",
            ),
        )
        database.connect().commit()
        project = database.fetch_one(
            "SELECT id FROM projects WHERE root_entry_id=?", (directory["id"],)
        )
        if project:
            upsert_relationship(
                database,
                "PROJECT",
                project["id"],
                "DIRECTORY",
                directory["id"],
                "CONTAINS",
                1.0,
                {"markers": found, "reproducibility": signals, "storage": breakdown},
                "1",
            )
        if job_id:
            update_job(
                database,
                job_id,
                "RUNNING",
                processed_count=index,
                checkpoint={"last_directory_id": int(directory["id"])},
            )
