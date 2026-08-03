"""Source-code / research project detection and regenerable-content accounting.

Detects roots from marker files; buckets recursive bytes as source / generated / environment.
``.git`` is always protected project state, never clutter.
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
    from ..path_utils import descendant_path_range

    prefix = f"{root_relative_path}/" if root_relative_path else ""
    low, high = descendant_path_range(root_relative_path)
    breakdown = {"source": 0, "generated": 0, "environment": 0, "file_count": 0}
    for row in database.iter_rows(
        "SELECT relative_path,size_bytes FROM filesystem_entries WHERE scan_run_id=? AND entry_type='file'"
        " AND relative_path>=? AND relative_path<?",
        (scan_run_id, low, high),
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
    from ..jobs import check_cancelled, checkpoint
    from ..relationships import upsert_relationship
    from .scope import resolve_scope

    entry_sql, params = resolve_scope(database, scope).entry_id_sql("directory")
    # One query for the stage, not one per directory. This asked every directory in the inventory
    # "what are your children called?" purely to intersect the answer with a 20-name set — 59,399
    # round trips on the real inventory to identify a few hundred projects. The intersection is a
    # join, so only candidate directories come back, each already carrying its markers.
    #
    # This is also why `directory_content` stays unbuilt (docs/performance.md): the per-directory
    # queries it was proposed to replace are gone, and the one genuinely recursive step below
    # already runs only for directories that turned out to be projects.
    markers = sorted(PROJECT_MARKERS)
    dirs = database.fetch_all(
        f"""SELECT e.id,e.name,e.relative_path,e.scan_run_id,
                   json_group_array(child.name) AS marker_names
            FROM filesystem_entries e
            JOIN filesystem_entries child
              ON child.scan_run_id=e.scan_run_id AND child.parent_entry_id=e.id
            WHERE e.entry_type='directory' AND e.id IN ({entry_sql})
              AND child.name IN ({",".join("?" for _ in markers)})
            GROUP BY e.id ORDER BY e.id""",
        (*params, *markers),
    )

    for index, directory in enumerate(dirs, start=1):
        if job_id:
            check_cancelled(database, job_id)
        found = sorted(set(json.loads(directory["marker_names"])))
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
        # No commit: the SELECT below runs on the same connection, so it sees the uncommitted
        # INSERT anyway. This was one transaction per detected project, and the stage already
        # commits once at the end.
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
        checkpoint(
            database, job_id, processed_count=index, state={"last_directory_id": int(directory["id"])}
        )
    # This analyser is a stage: the write primitives no longer commit per row, so the one
    # commit that makes its work durable belongs here.
    database.connect().commit()
