"""Typed analyzer registry and content-level, versioned artifact execution."""

from dataclasses import dataclass
import gzip
import hashlib
import json
from pathlib import Path
from datetime import datetime
from typing import Any, Callable

from ..config import AppConfig, config_fingerprint, performance_profile
from ..core.worker_pool import bounded_map, run_parser_isolated
from ..jobs import JobCancelled, JobPaused, check_cancelled, create_job, update_job
from ..database import Database
from ..hashing import compute_full_hash, compute_quick_hash
from .archives import inspect_archive
from .documents import extract_document
from .images import extract_image_metadata
from .images import create_thumbnail
from .media import extract_basic_media_metadata


@dataclass(frozen=True)
class AnalyzerSpec:
    name: str
    version: str
    suffixes: frozenset[str]
    runner: Callable[[Path, AppConfig], dict[str, Any]]
    timeout_seconds: int = 60


REGISTRY = (
    AnalyzerSpec(
        "documents",
        "2",
        frozenset(
            {".txt", ".md", ".csv", ".rst", ".log", ".docx", ".pdf", ".xlsx", ".xlsm", ".pptx"}
        ),
        lambda p, c: extract_document(p, p.suffix, c),
    ),
    AnalyzerSpec(
        "archives", "1", frozenset({".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz"}), inspect_archive
    ),
    AnalyzerSpec(
        "images",
        "1",
        frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".tiff", ".bmp"}),
        extract_image_metadata,
    ),
    AnalyzerSpec(
        "media",
        "1",
        frozenset({".mp3", ".wav", ".flac", ".m4a", ".ogg", ".mp4", ".mov", ".mkv", ".avi"}),
        extract_basic_media_metadata,
    ),
)


def specs() -> tuple[AnalyzerSpec, ...]:
    return REGISTRY


def _store_text(database: Database, content_id: int, text: str, config: AppConfig) -> int | None:
    if not config.section("content_store").get("store_normalized_text", True):
        return None
    raw = text.encode("utf-8")
    compression = "none"
    data = raw
    if config.section("content_store").get("compress_text", True):
        data = gzip.compress(raw)
        compression = "gzip"
    digest = hashlib.sha256(raw).hexdigest()
    cur = database.connect()
    cur.execute(
        "INSERT OR IGNORE INTO content_text_blobs(content_object_id,text_kind,compression,character_count,text_hash,data) VALUES(?,?,?,?,?,?)",
        (content_id, "normalized", compression, len(text), digest, data),
    )
    row = cur.execute(
        "SELECT id FROM content_text_blobs WHERE content_object_id=? AND text_kind=? AND text_hash=?",
        (content_id, "normalized", digest),
    ).fetchone()
    cur.commit()
    return int(row[0]) if row else None


def load_text_blob(database: Database, blob_id: int, maximum_characters: int = 5000) -> str:
    row = database.fetch_one(
        "SELECT compression,data FROM content_text_blobs WHERE id=?", (blob_id,)
    )
    if not row:
        raise ValueError("unknown text blob")
    data = bytes(row["data"])
    if row["compression"] == "gzip":
        data = gzip.decompress(data)
    return data.decode("utf-8", errors="replace")[:maximum_characters]


def _run_content_analysis(
    database: Database,
    config: AppConfig,
    analyzer_name: str | None = None,
    under: str | None = None,
    changed_only: bool = False,
    source_id: int | None = None,
    extensions: set[str] | None = None,
    size_min: int | None = None,
    size_max: int | None = None,
    older_than: str | None = None,
    newer_than: str | None = None,
    classification: str | None = None,
    content_object_ids: set[int] | None = None,
    scan_run_id: int | None = None,
    detected_mime: str | None = None,
    only_unique: bool = False,
    only_duplicate_candidates: bool = False,
    job_id: int | None = None,
) -> dict[str, int]:
    """Analyze each eligible content object once and make parser failures explicit."""
    wanted = [x for x in REGISTRY if analyzer_name in (None, "all", x.name)]
    counts = {"completed": 0, "skipped": 0, "errors": 0, "hashed": 0}
    # Establish content identity before parser selection.  An all-analysis pass therefore
    # covers every regular file, while a narrow analyzer only hashes its eligible suffixes.
    suffixes = set().union(*(spec.suffixes for spec in wanted)) if wanted else set()
    source_path = None
    if source_id is not None:
        source = database.fetch_one(
            "SELECT last_mount_path FROM source_roots WHERE id=?", (source_id,)
        )
        if not source:
            raise ValueError(f"unknown source id {source_id}")
        source_path = str(source["last_mount_path"])
    normalized_extensions = {
        value if value.startswith(".") else f".{value}" for value in (extensions or set())
    }
    older_timestamp = datetime.fromisoformat(older_than).timestamp() if older_than else None
    newer_timestamp = datetime.fromisoformat(newer_than).timestamp() if newer_than else None

    def in_scope(entry: Any, content_id: int | None = None) -> bool:
        if source_id is not None and entry["source_root_id"] != source_id:
            return False
        if scan_run_id is not None and entry["scan_run_id"] != scan_run_id:
            return False
        if detected_mime and entry["detected_mime"] != detected_mime:
            return False
        if source_path and not str(entry["absolute_path"]).startswith(source_path):
            return False
        if (
            normalized_extensions
            and str(entry["suffix"] or "").lower() not in normalized_extensions
        ):
            return False
        if size_min is not None and int(entry["size_bytes"] or 0) < size_min:
            return False
        if size_max is not None and int(entry["size_bytes"] or 0) > size_max:
            return False
        if older_timestamp is not None and (
            entry["modified_at"] is None or float(entry["modified_at"]) >= older_timestamp
        ):
            return False
        if newer_timestamp is not None and (
            entry["modified_at"] is None or float(entry["modified_at"]) <= newer_timestamp
        ):
            return False
        if classification and entry["classification"] != classification:
            return False
        return not content_object_ids or content_id in content_object_ids

    candidates = database.iter_rows(
        "SELECT e.id,e.scan_run_id,e.source_root_id,e.absolute_path,e.suffix,e.size_bytes,e.modified_at,c.classification,s.detected_mime FROM filesystem_entries e LEFT JOIN entry_content_links l ON l.entry_id=e.id LEFT JOIN classifications c ON c.entry_id=e.id LEFT JOIN file_signatures s ON s.entry_id=e.id WHERE e.entry_type='file' AND l.entry_id IS NULL ORDER BY e.id"
    )
    eligible = (
        dict(entry)
        for entry in candidates
        if (analyzer_name in (None, "all") or entry["suffix"] in suffixes) and in_scope(entry)
    )

    def hash_entry(entry: dict[str, Any]):
        try:
            target = Path(entry["absolute_path"])
            quick = compute_quick_hash(
                target,
                config.section("hashing")["quick_hash_chunk_bytes"],
                config.section("hashing")["quick_hash_middle_samples"],
                config.section("hashing")["algorithm"],
            )
            return (
                entry,
                quick,
                compute_full_hash(
                    target,
                    config.section("hashing")["algorithm"],
                    config.section("hashing")["full_hash_block_bytes"],
                ),
            )
        except OSError:
            return entry, None, None

    profile = performance_profile(config)
    queue_size = min(
        1_000, max(1, int(config.section("performance")["database_writer_queue_size"]))
    )
    for entry, quick, hashed in bounded_map(
        hash_entry, eligible, int(profile["full_hash_workers"]), queue_size
    ):
        if job_id:
            check_cancelled(database, job_id)
        try:
            if hashed is None:
                counts["errors"] += 1
                continue
            if not hashed.stable or not hashed.digest:
                counts["errors"] += 1
                continue
            content_id = database.get_or_create_content_object(
                config.section("hashing")["algorithm"],
                hashed.digest,
                hashed.size,
                entry["scan_run_id"],
            )
            database.link_entry_content(entry["id"], content_id, "")
            database.connect().execute(
                "INSERT OR REPLACE INTO file_signatures(entry_id,quick_hash,full_hash,hash_algorithm,hash_status,full_hash_computed_at) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP)",
                (
                    entry["id"],
                    quick.digest if quick and quick.stable else None,
                    hashed.digest,
                    config.section("hashing")["algorithm"],
                    "OK",
                ),
            )
            database.connect().commit()
            counts["hashed"] += 1
        except OSError:
            counts["errors"] += 1
    for spec in wanted:
        rows = database.iter_rows(
            """SELECT co.id,co.full_hash,co.size_bytes,e.id AS entry_id,e.scan_run_id,e.source_root_id,e.absolute_path,e.suffix,e.modified_at,c.classification,s.detected_mime,
            (SELECT COUNT(*) FROM entry_content_links links WHERE links.content_object_id=co.id AND links.link_status='VERIFIED') AS occurrence_count
            FROM content_objects co JOIN entry_content_links l ON l.content_object_id=co.id
            JOIN filesystem_entries e ON e.id=l.entry_id LEFT JOIN classifications c ON c.entry_id=e.id LEFT JOIN file_signatures s ON s.entry_id=e.id WHERE l.link_status='VERIFIED' AND e.entry_type='file'
            AND e.absolute_path NOT LIKE ? GROUP BY co.id ORDER BY co.id""",
            ("%/.housekeeper/%",),
        )
        for row in rows:
            if job_id:
                check_cancelled(database, job_id)
            if spec.suffixes and row["suffix"] not in spec.suffixes:
                continue
            if not in_scope(row, int(row["id"])):
                continue
            if only_unique and row["occurrence_count"] != 1:
                continue
            if only_duplicate_candidates and row["occurrence_count"] < 2:
                continue
            if under and not str(row["absolute_path"]).startswith(str(Path(under).resolve())):
                continue
            if changed_only and not database.fetch_one(
                "SELECT 1 FROM scan_entry_changes WHERE entry_id=? AND change_status NOT IN ('UNCHANGED') ORDER BY id DESC LIMIT 1",
                (row["entry_id"],),
            ):
                counts["skipped"] += 1
                continue
            fingerprint = config_fingerprint(config)
            if not database.fetch_one(
                "SELECT 1 FROM entry_content_links WHERE content_object_id=? AND link_status='VERIFIED'",
                (row["id"],),
            ):
                hashed_existing = compute_full_hash(
                    Path(row["absolute_path"]),
                    config.section("hashing")["algorithm"],
                    config.section("hashing")["full_hash_block_bytes"],
                )
                if not hashed_existing.stable or not hashed_existing.digest:
                    counts["errors"] += 1
                    continue
                content_id = database.get_or_create_content_object(
                    config.section("hashing")["algorithm"],
                    hashed_existing.digest,
                    hashed_existing.size,
                )
                entry_match = database.fetch_one(
                    "SELECT id FROM filesystem_entries WHERE absolute_path=? ORDER BY id DESC LIMIT 1",
                    (row["absolute_path"],),
                )
                if entry_match:
                    database.link_entry_content(entry_match["id"], content_id, "")
                    row = database.fetch_one(  # type: ignore[assignment]
                        "SELECT co.id,co.full_hash,co.size_bytes,e.id AS entry_id,e.absolute_path,e.suffix FROM content_objects co JOIN entry_content_links l ON l.content_object_id=co.id JOIN filesystem_entries e ON e.id=l.entry_id WHERE co.id=? LIMIT 1",
                        (content_id,),
                    )
                    if not row:
                        continue
            if database.is_analysis_current(row["id"], spec.name, spec.version, fingerprint):
                counts["skipped"] += 1
                continue
            try:
                # A content object can have several readable paths.  Retry a parser error
                # through another linked entry before recording a content-level failure.
                representatives = database.iter_rows(
                    """SELECT e.id,e.absolute_path FROM entry_content_links l JOIN filesystem_entries e ON e.id=l.entry_id
                       WHERE l.content_object_id=? AND l.link_status='VERIFIED' AND e.entry_type='file' ORDER BY e.id""",
                    (row["id"],),
                )
                result: dict[str, Any] | None = None
                representative_id: int | None = None
                last_error: str | None = None
                for representative in representatives:
                    try:
                        candidate = run_parser_isolated(
                            lambda: spec.runner(Path(representative["absolute_path"]), config),
                            min(
                                spec.timeout_seconds,
                                int(config.section("performance")["parser_timeout_seconds"]),
                            ),
                            int(config.section("performance")["parser_memory_limit_mb"]),
                        )
                    except (
                        Exception
                    ) as exc:  # isolated below as an artifact, never a recommendation
                        last_error = str(exc)
                        continue
                    state = candidate.get(
                        "analysis_status", candidate.get("extraction_status", "OK")
                    )
                    if state == "ERROR":
                        last_error = str(
                            candidate.get("analysis_error")
                            or candidate.get("extraction_error")
                            or "parser error"
                        )
                        continue
                    result, representative_id = candidate, int(representative["id"])
                    break
                if result is None:
                    result = {
                        "analysis_status": "ERROR",
                        "analysis_error": last_error or "no readable representative",
                    }
                result["representative_entry_id"] = representative_id
                if spec.name == "images" and representative_id is not None:
                    thumbnail = create_thumbnail(
                        Path(str(row["absolute_path"])), int(row["id"]), config
                    )
                    if thumbnail:
                        result["thumbnail_path"] = thumbnail
                status = (
                    "COMPLETED"
                    if result.get("analysis_status", result.get("extraction_status", "OK"))
                    not in {"ERROR", "UNSUPPORTED"}
                    else result.get("analysis_status", result.get("extraction_status"))
                )
                text_id = None
                if spec.name == "documents" and result.get("normalized_text"):
                    text_id = _store_text(database, row["id"], result["normalized_text"], config)
                database.connect().execute(
                    """INSERT OR REPLACE INTO analysis_artifacts(content_object_id,analyzer_name,analyzer_version,configuration_fingerprint,status,started_at,completed_at,artifact_json,text_blob_id,error_code,error_message)
                    VALUES(?,?,?,?,?,CURRENT_TIMESTAMP,CURRENT_TIMESTAMP,?,?,?,?)""",
                    (
                        row["id"],
                        spec.name,
                        spec.version,
                        fingerprint,
                        status,
                        json.dumps(result, sort_keys=True),
                        text_id,
                        None if status == "COMPLETED" else status,
                        result.get("analysis_error") or result.get("extraction_error"),
                    ),
                )
                database.connect().commit()
                counts["completed" if status == "COMPLETED" else "errors"] += 1
                if job_id:
                    update_job(
                        database,
                        job_id,
                        "RUNNING",
                        processed_count=counts["completed"] + counts["skipped"] + counts["errors"],
                        success_count=counts["completed"],
                        skip_count=counts["skipped"],
                        error_count=counts["errors"],
                        current_item=str(row["absolute_path"]),
                    )
            except Exception as exc:  # parser errors are protected artifacts, never recommendations
                database.connect().execute(
                    "INSERT OR REPLACE INTO analysis_artifacts(content_object_id,analyzer_name,analyzer_version,configuration_fingerprint,status,completed_at,error_code,error_message) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP,?,?)",
                    (
                        row["id"],
                        spec.name,
                        spec.version,
                        fingerprint,
                        "ERROR",
                        "ANALYZER_EXCEPTION",
                        str(exc),
                    ),
                )
                database.connect().commit()
                counts["errors"] += 1
    return counts


def run_content_analysis(
    database: Database,
    config: AppConfig,
    analyzer_name: str | None = None,
    under: str | None = None,
    changed_only: bool = False,
    source_id: int | None = None,
    extensions: set[str] | None = None,
    size_min: int | None = None,
    size_max: int | None = None,
    older_than: str | None = None,
    newer_than: str | None = None,
    classification: str | None = None,
    content_object_ids: set[int] | None = None,
    scan_run_id: int | None = None,
    detected_mime: str | None = None,
    only_unique: bool = False,
    only_duplicate_candidates: bool = False,
    job_id: int | None = None,
) -> dict[str, int]:
    job_types = {
        "documents": "DOCUMENT_ANALYSIS",
        "images": "IMAGE_ANALYSIS",
        "archives": "ARCHIVE_ANALYSIS",
        "media": "MEDIA_ANALYSIS",
    }
    managed_job_id = job_id or create_job(
        database,
        job_types.get(analyzer_name or "", "CONTENT_ANALYSIS"),
        {
            "analyzer": analyzer_name or "all",
            "under": under,
            "changed_only": changed_only,
            "source_id": source_id,
        },
        config_fingerprint(config),
        worker_count=int(performance_profile(config)["full_hash_workers"]),
    )
    if job_id is None:
        update_job(database, managed_job_id, "RUNNING")
    try:
        counts = _run_content_analysis(
            database,
            config,
            analyzer_name,
            under,
            changed_only,
            source_id,
            extensions,
            size_min,
            size_max,
            older_than,
            newer_than,
            classification,
            content_object_ids,
            scan_run_id,
            detected_mime,
            only_unique,
            only_duplicate_candidates,
            managed_job_id,
        )
    except (JobCancelled, JobPaused):
        raise
    except Exception:
        update_job(database, managed_job_id, "FAILED")
        raise
    if job_id is None:
        update_job(
            database,
            managed_job_id,
            "COMPLETED_WITH_ERRORS" if counts["errors"] else "COMPLETED",
            processed_count=counts["completed"] + counts["skipped"] + counts["errors"],
            success_count=counts["completed"],
            skip_count=counts["skipped"],
            error_count=counts["errors"],
        )
    return counts
