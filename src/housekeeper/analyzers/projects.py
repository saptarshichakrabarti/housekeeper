def classify_project_kind(markers: list[str]) -> str:
    return (
        "python"
        if any(x in markers for x in ("pyproject.toml", "requirements.txt"))
        else "javascript"
        if "package.json" in markers
        else "source"
    )


def run_project_analysis(database, config, scope=None, job_id: int | None = None):
    import json
    from ..relationships import upsert_relationship

    dirs = database.fetch_all(
        "SELECT id,name,relative_path,scan_run_id FROM filesystem_entries WHERE entry_type='directory'"
    )
    if scope:
        from .scope import scoped_entry_ids

        allowed = scoped_entry_ids(database, scope, "directory")
        dirs = [directory for directory in dirs if int(directory["id"]) in allowed]
    markers = {"pyproject.toml", "requirements.txt", "package.json", "Cargo.toml", "go.mod", ".git"}
    from ..jobs import check_cancelled, update_job

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
        found = sorted(names & markers)
        if not found:
            continue
        kind = classify_project_kind(found)
        database.connect().execute(
            "INSERT OR IGNORE INTO projects(root_entry_id,name,kind,markers_json) VALUES(?,?,?,?)",
            (directory["id"], directory["name"], kind, json.dumps(found)),
        )
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
                {"markers": found},
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
