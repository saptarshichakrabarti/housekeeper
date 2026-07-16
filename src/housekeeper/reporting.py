from pathlib import Path

from .config import AppConfig
from .database import Database


def generate_report(report_type: str, database: Database, config: AppConfig) -> Path:
    out = config.workspace / config.data["workspace"]["reports_dir"]
    out.mkdir(parents=True, exist_ok=True)
    path = out / (report_type + ".html")
    counts = database.fetch_all(
        "SELECT classification,COUNT(*) n FROM classifications GROUP BY classification"
    )
    files = database.fetch_one(
        "SELECT COUNT(*) n,COALESCE(SUM(size_bytes),0) b FROM filesystem_entries WHERE entry_type='file'"
    )
    assert files is not None
    body = "".join(f"<tr><td>{r['classification']}</td><td>{r['n']}</td></tr>" for r in counts)
    path.write_text(
        f"<!doctype html><meta charset='utf-8'><title>Housekeeper {report_type}</title><h1>{report_type}</h1><p>Files: {files['n']} &middot; Bytes: {files['b']}</p><table><tr><th>Classification</th><th>Count</th></tr>{body}</table>",
        encoding="utf-8",
    )
    return path


def generate_all_reports(database, config):
    return [
        generate_report(n, database, config)
        for n in (
            "summary",
            "inventory",
            "exact_duplicates",
            "directory_overlap",
            "document_versions",
            "image_groups",
            "large_files",
            "projects",
            "errors",
        )
    ]
