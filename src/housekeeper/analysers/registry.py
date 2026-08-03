"""Typed analyser registry and content-level, versioned artifact execution."""

import gzip
import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..config import AppConfig, config_fingerprint, performance_profile
from ..core import counters
from ..core.identity import ensure_content_identity, stream_identity_candidates
from ..core.worker_pool import bounded_map
from ..database import Database
from ..jobs import JobCancelled, JobPaused, check_cancelled, create_job, update_job
from .archives import inspect_archive
from .documents import extract_document
from .images import create_thumbnail, extract_image_metadata
from .media import extract_basic_media_metadata
from .parser_pool import ParserPool, worker_count
from .scope import AnalyserScope, resolve_scope


@dataclass(frozen=True)
class AnalyserSpec:
    name: str
    version: str
    suffixes: frozenset[str]
    runner: Callable[[Path, AppConfig], dict[str, Any]]
    timeout_seconds: int = 60
    #: The configuration sections this analyser's result can actually depend on. Only these are
    #: fingerprinted, so a change elsewhere does not invalidate its artifacts.
    config_sections: frozenset[str] = frozenset()


REGISTRY = (
    AnalyserSpec(
        "documents",
        "2",
        frozenset(
            {".txt", ".md", ".csv", ".rst", ".log", ".docx", ".pdf", ".xlsx", ".xlsm", ".pptx"}
        ),
        lambda p, c: extract_document(p, p.suffix, c),
        config_sections=frozenset({"documents", "content_store"}),
    ),
    AnalyserSpec(
        "archives",
        "1",
        frozenset({".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz"}),
        inspect_archive,
        config_sections=frozenset({"archives"}),
    ),
    AnalyserSpec(
        "images",
        # v2: the descriptor is a 16-hex 64-bit integer rather than a 64-character bit string, and
        # the artifact carries capture_time so clustering never re-opens the photograph.
        # v3: the descriptor is a DCT hash rather than an 8x8 average hash. Same width and encoding,
        # different measurement — so the version, not the format, is what supersedes v2 artifacts.
        "3",
        frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".tiff", ".bmp"}),
        extract_image_metadata,
        config_sections=frozenset({"images", "content_store"}),
    ),
    AnalyserSpec(
        "media",
        "1",
        frozenset({".mp3", ".wav", ".flac", ".m4a", ".ogg", ".mp4", ".mov", ".mkv", ".avi"}),
        extract_basic_media_metadata,
    ),
)


#: Artifact writes per transaction — smaller, because each one cost a parser run.
ARTIFACT_BATCH_SIZE = 50


def specs() -> tuple[AnalyserSpec, ...]:
    return REGISTRY


def spec_config_fingerprint(config: AppConfig, spec: AnalyserSpec) -> str:
    """Fingerprint only the configuration ``spec`` can read.

    Artifact reuse was keyed on the *entire* config, so changing ``dashboard.port`` invalidated
    every artifact in the inventory and forced a full corpus re-parse. A cache that invalidates on
    changes which cannot affect the result is indistinguishable from no cache. (Existing artifacts
    carry the old whole-config fingerprint, so the first run after this change re-analyses once.)
    """
    payload = {name: config.section(name) for name in sorted(spec.config_sections)}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()


def _work_plan(
    spec: AnalyserSpec,
    fingerprint: str,
    changed_only: bool,
    scope: AnalyserScope,
    pending_only: bool = True,
    maximum_size: int | None = None,
) -> tuple[str, tuple]:
    """Content objects this analyser still owes an artifact for — one query for the stage.

    Suffix, changed-only, scope, and the "artifact already current" anti-join are predicates
    here (was one query per object on a cache-hit rerun). ``pending_only=False`` drops the
    anti-join so the same shape counts eligibility vs skipped.

    Scope must be an ``EXISTS`` over current membership, not a Python post-filter: the old
    ``GROUP BY co.id`` picked an arbitrary historical representative, so after a rescan the
    planner could hand back an old snapshot row and silently skip work still owed.
    """
    clauses = [
        "l.link_status='VERIFIED'",
        "e.entry_type='file'",
        "e.absolute_path NOT LIKE '%/.housekeeper/%'",
    ]
    params: list[object] = []
    if spec.suffixes:
        clauses.append("e.suffix IN (" + ",".join("?" for _ in spec.suffixes) + ")")
        params.extend(sorted(spec.suffixes))
    if maximum_size is not None:
        # scanner.max_file_size_for_content_analysis: a ceiling on what a parser is handed, so one
        # enormous file cannot dominate a stage. Identity (hashing) is unaffected — every file is
        # still inventoried and still gets a digest.
        clauses.append("co.size_bytes<=?")
        params.append(maximum_size)
    if changed_only:
        clauses.append(
            "EXISTS(SELECT 1 FROM scan_entry_changes ch WHERE ch.entry_id=e.id AND ch.change_status<>'UNCHANGED')"
        )
    # Every scope facet — run ids, source, subtree, size, mime, classification, uniqueness — as one
    # EXISTS the planner resolves through the composite indexes, rather than a Python predicate run
    # against rows already fetched.
    scope_sql, scope_params = scope.entry_id_sql()
    clauses.append(f"e.id IN ({scope_sql})")
    params.extend(scope_params)
    if pending_only:
        clauses.append(
            """NOT EXISTS(SELECT 1 FROM analysis_artifacts a WHERE a.content_object_id=co.id
               AND a.analyser_name=? AND a.analyser_version=? AND a.configuration_fingerprint=?
               AND a.status='COMPLETED')"""
        )
        params.extend((spec.name, spec.version, fingerprint))
    # Every in-scope readable path for the object, in one column. A content object can have several,
    # and a parser error on one is retried through the next, so the stage needs the list — but it
    # used to fetch it with a query *per pending object*, which is the N+1 the single work plan was
    # supposed to remove. json_array encodes paths containing newlines correctly; group_concat
    # would not.
    return (
        """SELECT co.id,co.full_hash,co.size_bytes,
             json_group_array(json_array(e.id,e.absolute_path)) AS representatives_json
           FROM content_objects co JOIN entry_content_links l ON l.content_object_id=co.id
           JOIN filesystem_entries e ON e.id=l.entry_id
           WHERE """
        + " AND ".join(clauses)
        + " GROUP BY co.id ORDER BY co.id",
        tuple(params),
    )


def _count(reader, sql: str, params: tuple) -> int:
    row = reader.fetch_one(f"SELECT COUNT(*) AS n FROM ({sql})", params)
    return int(row["n"]) if row else 0


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
    # No commit: this runs once per parsed document, and committing here made the artifact batch
    # size a fiction — a 50-artifact batch became 50 transactions as soon as the documents analyser
    # produced text. The enclosing loop owns the transaction boundary.
    cur.execute(
        "INSERT OR IGNORE INTO content_text_blobs(content_object_id,text_kind,compression,character_count,text_hash,data) VALUES(?,?,?,?,?,?)",
        (content_id, "normalized", compression, len(text), digest, data),
    )
    row = cur.execute(
        "SELECT id FROM content_text_blobs WHERE content_object_id=? AND text_kind=? AND text_hash=?",
        (content_id, "normalized", digest),
    ).fetchone()
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
    analyser_name: str | None = None,
    scope: AnalyserScope | None = None,
    changed_only: bool = False,
    job_id: int | None = None,
) -> dict[str, int]:
    """analyse each eligible content object once and make parser failures explicit."""
    wanted = [x for x in REGISTRY if analyser_name in (None, "all", x.name)]
    counts = {"completed": 0, "skipped": 0, "errors": 0, "hashed": 0}
    # Establish content identity before parser selection.  An all-analysis pass therefore
    # covers every regular file, while a narrow analyser only hashes its eligible suffixes.
    suffixes = set().union(*(spec.suffixes for spec in wanted)) if wanted else set()
    # One scope object, used as a SQL predicate everywhere below. This stage used to take thirteen
    # loose filter arguments and apply them twice — once as loose SQL, once as a Python `in_scope`
    # re-check on rows already fetched — and the two disagreed. With no explicit run the SQL half
    # saw all history while the Python half compared against the requested run, so a rescan made
    # the stage skip work it genuinely owed, silently. There is now one representation of scope,
    # it defaults to the current inventory, and it is resolved by the database.
    scope = resolve_scope(database, scope)
    if scope.source_id is not None and not database.fetch_one(
        "SELECT 1 FROM source_roots WHERE id=?", (scope.source_id,)
    ):
        raise ValueError(f"unknown source id {scope.source_id}")

    entry_sql, entry_params = scope.entry_id_sql()
    # Ordered so inode-mates are adjacent: on a first full scan of a snapshot-style backup drive,
    # this is what lets the identity service read a hard-linked file once instead of once per
    # snapshot. Content-object id allocation order is not semantic (grouping orders by digest), so
    # the change is invisible downstream.
    # Keyset-paged so a multi-hour hashing stage never holds one read snapshot (which would pin the
    # WAL against the identity writer's per-batch commits); the ordering that keeps inode-mates
    # adjacent for hard-link reuse is the keyset's own order.
    candidates = stream_identity_candidates(
        database.reader(),
        f"""SELECT e.id,e.scan_run_id,e.absolute_path,e.suffix,e.size_bytes,e.device_id,e.inode_or_file_id,e.nlink
            FROM filesystem_entries e
            LEFT JOIN entry_content_links l ON l.entry_id=e.id
            WHERE e.entry_type='file' AND l.entry_id IS NULL AND e.id IN ({entry_sql}){{keyset}}""",
        entry_params,
    )
    eligible = (
        dict(entry)
        for entry in candidates
        if analyser_name in (None, "all") or entry["suffix"] in suffixes
    )

    # A denominator for the identity phase so a multi-day hash stage shows a percentage rather than a
    # bare climbing count. Counts unlinked files in scope: exact for an all-analyser pass (the common
    # case), a slight over-estimate for a single narrow analyser whose suffix filter the service
    # applies as it streams. The per-spec parse phase below revises the estimate to its own work.
    if job_id:
        identity_total = _count(
            database.reader(),
            f"""SELECT e.id FROM filesystem_entries e
                LEFT JOIN entry_content_links l ON l.entry_id=e.id
                WHERE e.entry_type='file' AND l.entry_id IS NULL AND e.id IN ({entry_sql})""",
            entry_params,
        )
        update_job(database, job_id, total_estimate=identity_total, current_item="identity")
    # Evict the page cache after hashing content the parse stage will never open — a file above the
    # analysis size ceiling, or a suffix no analyser handles — so a long scan of large media does not
    # push the database's own hot pages out of cache. Anything a parser *will* re-read is left cached.
    parse_ceiling = int(config.section("scanner")["max_file_size_for_content_analysis"])
    parseable_suffixes = set().union(*(spec.suffixes for spec in REGISTRY))

    def _drop_after_hash(entry: Mapping[str, Any]) -> bool:
        size = entry.get("size_bytes")
        if size is not None and int(size) > parse_ceiling:
            return True
        suffix = (entry.get("suffix") or "").lower()
        return suffix not in parseable_suffixes

    # Identity — full+quick digest, content-object link, signature row — for every eligible file,
    # hashed across `full_hash_workers` threads by the one shared service. It commits per batch (the
    # per-spec sweep below reads the work plan on an independent read-only connection, so identity
    # must be durable before it starts) and records this source's hashing throughput so
    # `storage_profile: auto` can prefer a measurement over a path guess next run.
    identity = ensure_content_identity(
        database,
        config,
        eligible,
        job_id,
        workers=int(performance_profile(config)["full_hash_workers"]),
        record_errors=False,
        drop_cache=_drop_after_hash,
        progress_phase="identity",
    )
    counts["hashed"] += identity["hashed"]
    counts["errors"] += identity["errors"]
    # One pool for the whole stage. Workers persist across parses, so the 8–11 ms of process
    # creation that used to precede every single parse is paid once per worker instead.
    parsers = ParserPool(
        config,
        worker_count(config),
        int(config.section("performance")["parser_memory_limit_mb"]),
    )
    artifacts_since_commit = 0
    # The parse phase's own progress denominator, accumulated as each spec is planned. Without this
    # the job keeps the *identity* phase's total, and the bar reads nonsense ("100% 40/1") the moment
    # the parse loop processes more objects than the identity phase had files left to hash. Mirrors
    # the exact-duplicates precedent: a job with two phases revises its total at the phase boundary.
    parse_planned = 0
    try:
        for spec in wanted:
            # Hoisted out of the per-row loop below, where it re-hashed the whole configuration once
            # per content object (~188 s per million objects, for a value that cannot change mid-run).
            fingerprint = spec_config_fingerprint(config, spec)
            maximum_size = int(config.section("scanner")["max_file_size_for_content_analysis"])
            plan_sql, plan_params = _work_plan(
                spec, fingerprint, changed_only, scope, maximum_size=maximum_size
            )
            # Objects whose artifact is already current never reach the loop now, so the skipped total
            # and the cache-hit counters come from two COUNTs rather than a query per object.
            reader = database.reader()
            pending = _count(reader, plan_sql, plan_params)
            eligible_total = _count(
                reader,
                *_work_plan(
                    spec,
                    fingerprint,
                    changed_only,
                    scope,
                    pending_only=False,
                    maximum_size=maximum_size,
                ),
            )
            counts["skipped"] += max(0, eligible_total - pending)
            counters.count("artifact_cache_hits", max(0, eligible_total - pending))
            counters.count("artifact_cache_misses", pending)
            parse_planned += eligible_total
            if job_id:
                update_job(
                    database,
                    job_id,
                    total_estimate=parse_planned,
                    current_item=f"analysing {spec.name}",
                )
            timeout = min(
                spec.timeout_seconds,
                int(config.section("performance")["parser_timeout_seconds"]),
            )

            def eligible_work(plan_sql=plan_sql, plan_params=plan_params):
                """Work plan streamed on a read-only connection; representatives already decoded.

                Writer commits artifacts while this cursor streams. Parser threads see plain data
                only — no SQLite across threads; scope was resolved in SQL.
                """
                for row in database.reader().iter_rows(plan_sql, plan_params):
                    if job_id:
                        check_cancelled(database, job_id)
                    representatives = sorted(
                        (int(entry_id), str(path))
                        for entry_id, path in json.loads(row["representatives_json"])
                    )
                    yield row, representatives

            def parse_one(item, spec=spec, timeout=timeout):
                """One content object end-to-end on a submitter thread; touches no database.

                Tries readable paths in order; a path-level parser error falls through to the
                next before recording a content-level failure.
                """
                row, representatives = item
                result: dict[str, Any] | None = None
                representative_id: int | None = None
                representative_path: str | None = None
                last_error: str | None = None
                for entry_id, absolute_path in representatives:
                    try:
                        candidate = parsers.run(spec.name, absolute_path, timeout)
                    except Exception as exc:  # noqa: BLE001 - recorded as an artifact, never a recommendation
                        last_error = str(exc)
                        continue
                    state = candidate.get("analysis_status", candidate.get("extraction_status", "OK"))
                    if state == "ERROR":
                        last_error = str(
                            candidate.get("analysis_error")
                            or candidate.get("extraction_error")
                            or "parser error"
                        )
                        continue
                    result, representative_id, representative_path = candidate, entry_id, absolute_path
                    break
                if result is None:
                    result = {
                        "analysis_status": "ERROR",
                        "analysis_error": last_error or "no readable representative",
                    }
                    representative_path = representatives[0][1] if representatives else None
                return row, result, representative_id, representative_path

            # `parser_workers` now sizes the work in flight, not just the pool. A pool of N is
            # worth N only if N parses are outstanding; submitted one at a time it was worth one.
            # Every database write stays on this thread, in completion order.
            for row, result, representative_id, representative_path in bounded_map(
                parse_one, eligible_work(), parsers.workers, parsers.workers * 4
            ):
                if job_id:
                    check_cancelled(database, job_id)
                try:
                    result["representative_entry_id"] = representative_id
                    if spec.name == "images" and representative_path is not None:
                        # The path that actually parsed, not an arbitrary sibling: a thumbnail is
                        # only meaningful for the copy the metadata came from.
                        thumbnail = create_thumbnail(
                            Path(representative_path), int(row["id"]), config
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
                        """INSERT OR REPLACE INTO analysis_artifacts(content_object_id,analyser_name,analyser_version,configuration_fingerprint,status,started_at,completed_at,artifact_json,text_blob_id,error_code,error_message)
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
                    counts["completed" if status == "COMPLETED" else "errors"] += 1
                    # Count every durable outcome, not only successful ones. With an error-only
                    # corpus `completed` stays zero, and `0 % batch_size == 0` used to commit every
                    # artifact — exactly the fsync amplification batching exists to prevent.
                    artifacts_since_commit += 1
                    if artifacts_since_commit >= ARTIFACT_BATCH_SIZE:
                        database.connect().commit()
                        artifacts_since_commit = 0
                    if job_id:
                        update_job(
                            database,
                            job_id,
                            processed_count=counts["completed"] + counts["skipped"] + counts["errors"],
                            success_count=counts["completed"],
                            skip_count=counts["skipped"],
                            error_count=counts["errors"],
                            current_item=representative_path or "",
                        )
                except Exception as exc:  # noqa: BLE001 - parser errors are protected artifacts
                    database.connect().execute(
                        "INSERT OR REPLACE INTO analysis_artifacts(content_object_id,analyser_name,analyser_version,configuration_fingerprint,status,completed_at,error_code,error_message) VALUES(?,?,?,?,?,CURRENT_TIMESTAMP,?,?)",
                        (
                            row["id"],
                            spec.name,
                            spec.version,
                            fingerprint,
                            "ERROR",
                            "ANALYSER_EXCEPTION",
                            str(exc),
                        ),
                    )
                    counts["errors"] += 1
                    # Exceptional post-processing is still an analysis outcome and follows the
                    # same transaction policy as COMPLETED/ERROR/UNSUPPORTED parser results.
                    artifacts_since_commit += 1
                    if artifacts_since_commit >= ARTIFACT_BATCH_SIZE:
                        database.connect().commit()
                        artifacts_since_commit = 0
        database.connect().commit()
    finally:
        parsers.close()
    return counts


def run_content_analysis(
    database: Database,
    config: AppConfig,
    analyser_name: str | None = None,
    scope: AnalyserScope | None = None,
    changed_only: bool = False,
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
        job_types.get(analyser_name or "", "CONTENT_ANALYSIS"),
        {
            "analyser": analyser_name or "all",
            "under": scope.under if scope else None,
            "changed_only": changed_only,
            "source_id": scope.source_id if scope else None,
        },
        config_fingerprint(config),
        worker_count=int(performance_profile(config)["full_hash_workers"]),
    )
    if job_id is None:
        update_job(database, managed_job_id, "RUNNING")
    try:
        counts = _run_content_analysis(
            database, config, analyser_name, scope, changed_only, managed_job_id
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
