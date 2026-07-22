"""User-configurable functional record series (personal organizational categories).

These are advisory personal categories, not legal retention determinations. Automated
assignment always carries a confidence; ambiguous items default to UNKNOWN for review.
"""

from __future__ import annotations

import json

DEFAULT_SERIES = [
    "ADMINISTRATIVE",
    "FINANCIAL_AND_TAX",
    "ACADEMIC_RECORDS",
    "RESEARCH_PROJECTS",
    "PUBLICATIONS",
    "EMPLOYMENT",
    "CORRESPONDENCE",
    "PERSONAL_PHOTOGRAPHS",
    "PERSONAL_VIDEO",
    "SOFTWARE_INSTALLERS",
    "DEVICE_BACKUPS",
    "SOURCE_CODE",
    "DATASETS",
    "REFERENCE_MATERIAL",
    "TEMPORARY_EXPORTS",
    "GENERATED_BUILD_ARTIFACTS",
    "UNKNOWN",
]

_IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".tiff", ".heic", ".bmp", ".webp"}
_VIDEO = {".mp4", ".mov", ".mkv", ".avi", ".m4v"}
_INSTALLER = {".exe", ".msi", ".dmg", ".pkg", ".deb", ".rpm", ".iso", ".appimage"}
_CODE = {".py", ".js", ".ts", ".c", ".cpp", ".h", ".rs", ".go", ".java", ".rb", ".sh"}
_DATA = {".csv", ".tsv", ".parquet", ".jsonl", ".sqlite", ".db", ".hdf5", ".npz"}
_GENERATED_DIRS = {"__pycache__", "dist", "build", "target", "node_modules", ".venv", "venv"}


def seed_default_series(database) -> None:
    for name in DEFAULT_SERIES:
        database.connect().execute(
            "INSERT OR IGNORE INTO record_series(name,description) VALUES(?,?)",
            (name, f"Default series: {name}"),
        )
    database.connect().commit()


def _series_ids(database) -> dict[str, int]:
    return {r["name"]: int(r["id"]) for r in database.fetch_all("SELECT id,name FROM record_series")}


def classify_series(name: str, suffix: str, relative_path: str) -> tuple[str, float]:
    """Return (series, confidence). Conservative: ambiguous -> UNKNOWN (review)."""
    lower_name = name.lower()
    lower_path = relative_path.lower().replace("\\", "/")
    segments = set(lower_path.split("/")[:-1])
    if segments & _GENERATED_DIRS:
        return "GENERATED_BUILD_ARTIFACTS", 0.85
    if suffix in _INSTALLER:
        return "SOFTWARE_INSTALLERS", 0.9
    if any(token in lower_name for token in ("tax", "invoice", "receipt", "statement", "1099", "w2")):
        return "FINANCIAL_AND_TAX", 0.7
    if suffix in _VIDEO:
        return "PERSONAL_VIDEO", 0.6
    if suffix in _IMAGE:
        return "PERSONAL_PHOTOGRAPHS", 0.6
    if suffix in _CODE:
        return "SOURCE_CODE", 0.75
    if suffix in _DATA:
        return "DATASETS", 0.6
    if any(token in lower_path for token in ("backup", "time machine", "device backup")):
        return "DEVICE_BACKUPS", 0.6
    if any(token in lower_path for token in ("/tmp/", "/temp/", "export", "download")):
        return "TEMPORARY_EXPORTS", 0.5
    return "UNKNOWN", 0.3


def run_record_series_analysis(database, config, scope=None, job_id=None) -> dict[str, int]:
    from ..jobs import checkpoint

    seed_default_series(database)
    series_ids = _series_ids(database)
    counts: dict[str, int] = {}
    conn = database.connect()
    conn.execute("DELETE FROM record_series_assignments WHERE target_type='ENTRY'")
    batch = []
    processed = 0
    for row in database.iter_rows(
        "SELECT id,name,suffix,relative_path FROM filesystem_entries WHERE entry_type='file'"
    ):
        processed += 1
        series, confidence = classify_series(
            row["name"], (row["suffix"] or "").lower(), row["relative_path"]
        )
        counts[series] = counts.get(series, 0) + 1
        batch.append(
            (
                "ENTRY",
                int(row["id"]),
                series_ids[series],
                confidence,
                json.dumps({"suffix": row["suffix"]}),
                "analyser",
            )
        )
        if len(batch) >= 1000:
            conn.executemany(
                "INSERT OR IGNORE INTO record_series_assignments(target_type,target_id,series_id,confidence,evidence_json,source) VALUES(?,?,?,?,?,?)",
                batch,
            )
            batch.clear()
            checkpoint(database, job_id, processed_count=processed)
    if batch:
        conn.executemany(
            "INSERT OR IGNORE INTO record_series_assignments(target_type,target_id,series_id,confidence,evidence_json,source) VALUES(?,?,?,?,?,?)",
            batch,
        )
    conn.commit()
    return counts


def assign_series_to_collection(database, collection_id: int, series_id: int) -> None:
    database.connect().execute(
        "INSERT OR IGNORE INTO record_series_assignments(target_type,target_id,series_id,confidence,source) VALUES('COLLECTION',?,?,1.0,'user')",
        (collection_id, series_id),
    )
    database.connect().commit()
