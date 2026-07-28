"""Event and collection clustering (photo events, work sessions).

Photos and work products are reviewed as collections, not isolated files. Clustering uses time
gaps (EXIF capture time when available, else file modification time). Precise GPS is never
stored or displayed by default — only time/sequence signals are used.

Capture time is read from the image artifact recorded at parse time, not from the file: this used
to open every photograph with PIL on every run, once per snapshot of it.
"""

from __future__ import annotations

import json

from ..constants import ClusterType

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".tiff", ".heic", ".bmp", ".webp"}
_DOC_SUFFIXES = {".txt", ".md", ".doc", ".docx", ".pdf", ".pptx", ".xlsx"}

#: Capture time from the newest images artifact linked to this entry, if one exists. Entries with
#: no image analysis fall back to modification time, exactly as an unreadable EXIF block did.
_CAPTURE_TIME_SQL = """(SELECT json_extract(a.artifact_json,'$.capture_time')
     FROM entry_content_links l
     JOIN analysis_artifacts a ON a.content_object_id=l.content_object_id
     WHERE l.entry_id=e.id AND a.analyser_name='images' AND a.status='COMPLETED'
     ORDER BY a.id DESC LIMIT 1)"""


def _cluster(database, cluster_type: str, suffixes: set[str] | None, gap_seconds: float, use_exif: bool, job_id: int | None = None, scope=None) -> int:
    from ..analysers.scope import resolve_scope
    from ..jobs import checkpoint

    # Scoped to the current inventory. Unscoped, every snapshot of the same photograph joins the
    # same timeline, so a "photo event" is padded with re-scans of one picture — a correctness bug
    # rather than merely a slow one.
    entry_sql, params = resolve_scope(database, scope).entry_id_sql()
    clauses = [f"e.entry_type='file' AND e.id IN ({entry_sql})"]
    query_params = list(params)
    if suffixes is not None:
        clauses.append("lower(e.suffix) IN (" + ",".join("?" for _ in suffixes) + ")")
        query_params.extend(sorted(suffixes))
    capture = _CAPTURE_TIME_SQL if use_exif else "NULL"
    rows = database.fetch_all(
        f"SELECT e.id,e.modified_at,{capture} AS capture_time FROM filesystem_entries e "
        "WHERE " + " AND ".join(clauses),
        tuple(query_params),
    )
    timed: list[tuple[int, float]] = []
    for row in rows:
        timestamp = row["capture_time"]
        if timestamp is None:
            timestamp = row["modified_at"]
        if timestamp is None:
            continue
        timed.append((int(row["id"]), float(timestamp)))
    # Entry id breaks timestamp ties, so cluster membership does not depend on row order.
    timed.sort(key=lambda item: (item[1], item[0]))
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
            database, ClusterType.PHOTO_EVENT, _IMAGE_SUFFIXES, gap, True, job_id, scope
        )
    }


def run_work_session_analysis(database, config, scope=None, job_id=None) -> dict[str, int]:
    gap = float(config.section("collections")["work_session_gap_hours"]) * 3600
    return {
        "work_sessions": _cluster(
            database, ClusterType.WORK_SESSION, _DOC_SUFFIXES, gap, False, job_id, scope
        )
    }


def run_acquisition_batch_analysis(database, config, scope=None, job_id=None) -> dict[str, int]:
    """Cluster download-like material of any type by close acquisition/modification time."""
    gap = float(config.section("collections")["acquisition_batch_gap_minutes"]) * 60
    return {
        "acquisition_batches": _cluster(
            database, ClusterType.ACQUISITION_BATCH, None, gap, False, job_id, scope
        )
    }
