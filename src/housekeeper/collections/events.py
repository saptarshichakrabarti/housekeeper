"""Event and collection clustering (photo events, work sessions).

Photos and work products are reviewed as collections, not isolated files. Clustering uses time
gaps (EXIF capture time when available, else file modification time). Precise GPS is never
stored or displayed by default — only time/sequence signals are used.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from ..constants import ClusterType

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".tiff", ".heic", ".bmp", ".webp"}
_DOC_SUFFIXES = {".txt", ".md", ".doc", ".docx", ".pdf", ".pptx", ".xlsx"}


def _capture_time(path: Path) -> float | None:
    try:
        from PIL import Image

        with Image.open(path) as image:
            exif = getattr(image, "_getexif", lambda: None)() or {}
        stamp = exif.get(36867) or exif.get(306)  # DateTimeOriginal / DateTime (never GPS)
        if stamp:
            return time.mktime(time.strptime(str(stamp), "%Y:%m:%d %H:%M:%S"))
    except Exception:  # noqa: BLE001 - EXIF is best-effort; fall back to file time
        return None
    return None


def _cluster(database, cluster_type: str, suffixes: set[str] | None, gap_seconds: float, use_exif: bool, job_id: int | None = None) -> int:
    from ..jobs import checkpoint

    rows = database.fetch_all(
        "SELECT id,absolute_path,modified_at,suffix FROM filesystem_entries WHERE entry_type='file'"
    )
    timed: list[tuple[int, float]] = []
    for row in rows:
        if suffixes is not None and (row["suffix"] or "").lower() not in suffixes:
            continue
        timestamp = None
        if use_exif:
            timestamp = _capture_time(Path(row["absolute_path"]))
        if timestamp is None:
            timestamp = row["modified_at"]
        if timestamp is None:
            continue
        timed.append((int(row["id"]), float(timestamp)))
    timed.sort(key=lambda item: item[1])
    created = 0
    previous = 0.0
    groups: list[list[int]] = []
    for entry_id, timestamp in timed:
        if groups and timestamp - previous <= gap_seconds:
            groups[-1].append(entry_id)
        else:
            groups.append([entry_id])
        previous = timestamp
    for number, members in enumerate(groups, start=1):
        checkpoint(database, job_id, processed_count=number, state={"clusters_seen": number})
        if len(members) < 2:
            continue
        name = f"{cluster_type.lower()}-{number:04d}"
        database.connect().execute(
            """INSERT INTO collection_clusters(cluster_type,name,algorithm,summary_json)
               VALUES(?,?,?,?)
               ON CONFLICT(cluster_type,name) DO UPDATE SET summary_json=excluded.summary_json""",
            (cluster_type, name, "time_gap", json.dumps({"member_count": len(members)}, sort_keys=True)),
        )
        cluster = database.fetch_one(
            "SELECT id FROM collection_clusters WHERE cluster_type=? AND name=?", (cluster_type, name)
        )
        database.connect().execute(
            "DELETE FROM collection_members WHERE cluster_id=?", (cluster["id"],)
        )
        database.connect().executemany(
            "INSERT OR IGNORE INTO collection_members(cluster_id,member_type,member_id,sequence_index) VALUES(?,?,?,?)",
            [(cluster["id"], "ENTRY", member, seq) for seq, member in enumerate(members)],
        )
        created += 1
    database.connect().commit()
    return created


def run_photo_event_analysis(database, config, scope=None, job_id=None) -> dict[str, int]:
    gap = float(config.section("collections")["photo_event_gap_minutes"]) * 60
    return {
        "photo_events": _cluster(
            database, ClusterType.PHOTO_EVENT, _IMAGE_SUFFIXES, gap, True, job_id
        )
    }


def run_work_session_analysis(database, config, scope=None, job_id=None) -> dict[str, int]:
    gap = float(config.section("collections")["work_session_gap_hours"]) * 3600
    return {
        "work_sessions": _cluster(
            database, ClusterType.WORK_SESSION, _DOC_SUFFIXES, gap, False, job_id
        )
    }


def run_acquisition_batch_analysis(database, config, scope=None, job_id=None) -> dict[str, int]:
    """Cluster download-like material of any type by close acquisition/modification time."""
    gap = float(config.section("collections")["acquisition_batch_gap_minutes"]) * 60
    return {
        "acquisition_batches": _cluster(
            database, ClusterType.ACQUISITION_BATCH, None, gap, False, job_id
        )
    }
