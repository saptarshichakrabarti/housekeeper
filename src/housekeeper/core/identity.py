"""One shared, parallel content-identity service.

Establishing a file's identity — its full digest, its quick digest, its content-object link and
signature row — used to be written out in six different loops, five of them serial on the main
thread with :func:`compute_full_hash`, one file at a time. The worst of them ran *before* the one
loop that had a worker pool, so on a first scan of a media drive the pool inherited only the
leftovers. This is that work in one place: fed a stream of candidate rows, it hashes them across
``full_hash_workers`` threads (``hashlib`` releases the GIL, so this scales), and does every database
write on the caller thread in completion order.

Two digests come from **one** sequential read (:func:`compute_identity`) — the quick digest is a
by-product of the bytes the full hash already went past, never a second pass — which is why the
serial ``compute_full_hash`` sites gain a quick hash for free by moving here.

The write shape matches what each caller wrote before:

* ``record_errors=False`` (the content-analysis path): a stable digest is linked and signed; an
  unstable read or an ``OSError`` is counted and skipped, no signature row.
* ``record_errors=True`` (the duplicate-candidate path): the same, plus a ``hash_status='ERROR'``
  signature row carrying the error, so a file that could not be read is recorded as tried, not
  silently absent from the funnel.

**The service streams.** It never materialises the candidate list, so a first scan of a
hundred-million-file drive holds only ``queue_size`` reads in flight, exactly as the loop it
replaces did. Hard-link identity reuse rides that stream: inode-mates that are *adjacent* in it —
which the caller arranges by ordering on ``(device_id, inode_or_file_id)`` — are read once and the
result written for the whole run, so a snapshot-style backup drive never reads the same physical
bytes twice. Reuse is gated on ``nlink > 1`` (the filesystem's own assertion that the paths share
storage). A hard link that is *not* adjacent is simply hashed again and linked to the same content
object: reuse is an optimisation with no correctness stake, so best-effort adjacency is enough.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path
from typing import Any

from ..config import AppConfig, performance_profile
from ..database import Database
from ..hashing import compute_identity
from ..jobs import check_cancelled, update_job
from .worker_pool import bounded_map

#: Identity writes per transaction. Bounded so an interrupted run loses at most this much work.
IDENTITY_BATCH_SIZE = 500

#: Below this the elapsed time is dominated by pool startup rather than by the storage, so the
#: quotient says nothing about the drive and is not recorded as a throughput observation.
THROUGHPUT_SAMPLE_MINIMUM_BYTES = 32 * 1024 * 1024


def _field(row: Mapping[str, Any], key: str) -> Any:
    """Read ``key`` from a dict or a ``sqlite3.Row``, returning ``None`` when it is absent.

    Callers stream rows of different shapes (some select ``scan_run_id``, some do not); a tolerant
    accessor lets the one service consume all of them without each caller normalising first.
    """
    try:
        return row[key]
    except (IndexError, KeyError):
        return None


def record_identity_throughput(database: Database, hashed_bytes: int, seconds: float) -> None:
    """Attribute this stage's hashing throughput to the source roots it read from.

    Attributed per source root because that is what the observation is *about* — two drives in one
    workspace have different speeds, and a single number for the workspace would average an SSD with
    a network share into a profile that is wrong for both. Only over enough bytes to swamp process
    startup, or the number is noise.
    """
    if hashed_bytes < THROUGHPUT_SAMPLE_MINIMUM_BYTES or seconds <= 0:
        return
    rate = hashed_bytes / seconds
    for row in database.fetch_all(
        """SELECT DISTINCT r.source_root_fingerprint AS fingerprint
           FROM scan_runs r JOIN source_roots s ON s.source_fingerprint=r.source_root_fingerprint
           WHERE s.latest_complete_scan_run_id=r.id"""
    ):
        database.record_hash_throughput(str(row["fingerprint"]), rate)


# The identity-candidate ordering: inode-mates adjacent (for hard-link reuse), tail-broken on id.
# COALESCE so NULL device/inode sort deterministically and the keyset row-value comparison is
# well-defined — st_dev/st_ino are never negative, so -1 is a safe sentinel below every real value.
_IDENTITY_KEY_EXPRS = ("COALESCE(e.device_id,-1)", "COALESCE(e.inode_or_file_id,-1)", "e.id")


def _identity_boundary(row: Mapping[str, Any]) -> tuple[int, int, int]:
    device = _field(row, "device_id")
    inode = _field(row, "inode_or_file_id")
    return (device if device is not None else -1, inode if inode is not None else -1, int(row["id"]))


def stream_identity_candidates(reader, sql: str, params: tuple, batch_size: int = 5_000):
    """Keyset-paged stream of identity candidates, ordered so inode-mates stay adjacent.

    ``sql`` must SELECT ``e.id, e.device_id, e.inode_or_file_id`` (the ordering columns), carry no
    ``ORDER BY``, and contain ``{keyset}`` in its ``WHERE``. Paging keeps a long hashing stage from
    pinning the WAL while preserving the hard-link adjacency the reuse depends on.
    """
    return reader.iter_keyset(
        sql, params, key_exprs=_IDENTITY_KEY_EXPRS, key_of=_identity_boundary, batch_size=batch_size
    )


def _inode_key(entry: Mapping[str, Any]) -> tuple[int, int] | None:
    """The ``(device, inode)`` identity of a file with more than one hard link, or ``None``.

    ``None`` for anything that is not provably a hard link to shared storage — a single-link file, or
    one whose device/inode the scan could not read — so those always hash on their own.
    """
    nlink = _field(entry, "nlink")
    device = _field(entry, "device_id")
    inode = _field(entry, "inode_or_file_id")
    if not nlink or int(nlink) <= 1 or device is None or inode is None:
        return None
    return (int(device), int(inode))


def _grouped(
    candidates: Iterable[Mapping[str, Any]], hardlink_reuse: bool
) -> Iterator[tuple[Mapping[str, Any], list[Mapping[str, Any]]]]:
    """Stream ``(representative, followers)`` units, holding at most one inode run in memory.

    Inode-mates that are adjacent in ``candidates`` share the representative's hash; every other
    entry is its own unit with no followers. Because a run is emitted only when the next
    non-matching entry arrives, the followers of a representative are always complete before that
    unit is yielded — which is what makes the reuse safe under the out-of-order worker pool.
    """
    run_key: tuple[int, int] | None = None
    representative: Mapping[str, Any] | None = None
    followers: list[Mapping[str, Any]] = []
    for entry in candidates:
        key = _inode_key(entry) if hardlink_reuse else None
        if key is not None and key == run_key and representative is not None:
            followers.append(entry)
            continue
        if representative is not None:
            yield representative, followers
        representative, run_key, followers = entry, key, []
    if representative is not None:
        yield representative, followers


def ensure_content_identity(
    database: Database,
    config: AppConfig,
    candidates: Iterable[Mapping[str, Any]],
    job_id: int | None = None,
    *,
    workers: int | None = None,
    record_errors: bool = False,
    record_throughput: bool = True,
    hardlink_reuse: bool | None = None,
    drop_cache: Callable[[Mapping[str, Any]], bool] | None = None,
    progress_phase: str = "hashing",
) -> dict[str, int]:
    """Hash every candidate once, link and sign it, and return ``{hashed, errors, bytes, reused}``.

    ``candidates`` yields mappings with at least ``id`` and ``absolute_path``; ``scan_run_id``,
    ``device_id``, ``inode_or_file_id`` and ``nlink`` are used when present. Every database write
    happens on the caller thread, committed once per :data:`IDENTITY_BATCH_SIZE` files so an
    interruption loses at most that much. To benefit from hard-link reuse the caller must order the
    stream so inode-mates are adjacent (``ORDER BY device_id, inode_or_file_id``).
    """
    hashing = config.section("hashing")
    algorithm = hashing["algorithm"]
    if workers is None:
        workers = int(performance_profile(config)["full_hash_workers"])
    queue_size = min(
        1_000, max(1, int(config.section("performance")["database_writer_queue_size"]))
    )
    if hardlink_reuse is None:
        hardlink_reuse = bool(hashing.get("hardlink_identity_reuse", True))

    def hash_unit(unit: tuple[Mapping[str, Any], list[Mapping[str, Any]]]):
        entry, followers = unit
        try:
            full, quick = compute_identity(
                Path(str(entry["absolute_path"])),
                algorithm,
                hashing["full_hash_block_bytes"],
                hashing["quick_hash_chunk_bytes"],
                hashing["quick_hash_middle_samples"],
                drop_cache=bool(drop_cache(entry)) if drop_cache is not None else False,
            )
            return entry, followers, quick, full
        except OSError:
            return entry, followers, None, None

    counts = {"hashed": 0, "errors": 0, "bytes": 0, "reused": 0}
    # Followers whose hard-link representative failed to hash. They cannot inherit a digest that was
    # never computed, so re-attempt each on its own path rather than dropping it — a per-path
    # permission or transient error on the representative need not doom its inode-mates, and even a
    # genuinely unreadable inode must leave every follower with an error signature, not silence.
    orphaned_followers: list[Mapping[str, Any]] = []
    started = time.perf_counter()

    def write_signature(entry_id: int, quick, hashed, status: str, error: str | None) -> None:
        database.connect().execute(
            "INSERT OR REPLACE INTO file_signatures(entry_id,quick_hash,full_hash,hash_algorithm,"
            "hash_status,hash_error,full_hash_computed_at) VALUES(?,?,?,?,?,?,CURRENT_TIMESTAMP)",
            (
                entry_id,
                quick.digest if quick and quick.stable else None,
                hashed.digest if hashed and hashed.stable else None,
                algorithm,
                status,
                error,
            ),
        )

    def link_success(entry: Mapping[str, Any], quick, hashed) -> None:
        content_id = database.get_or_create_content_object(
            algorithm, hashed.digest, hashed.size, _field(entry, "scan_run_id")
        )
        database.link_entry_content(int(entry["id"]), content_id, "")
        write_signature(int(entry["id"]), quick, hashed, "OK", None)

    for entry, followers, quick, hashed in bounded_map(
        hash_unit, _grouped(candidates, hardlink_reuse), workers, queue_size
    ):
        if job_id:
            check_cancelled(database, job_id)
        try:
            if hashed is None or not hashed.stable or not hashed.digest:
                counts["errors"] += 1
                if record_errors:
                    error = hashed.error if hashed is not None else "unreadable"
                    write_signature(int(entry["id"]), quick, hashed, "ERROR", error)
                # The representative failed, so its inode-mates have no digest to inherit; re-hash
                # them individually below rather than let them fall out of the run unsigned.
                orphaned_followers.extend(followers)
                continue
            link_success(entry, quick, hashed)
            counts["hashed"] += 1
            counts["bytes"] += int(hashed.size or 0)
            # Every path that shares this file's inode gets the identical result written from
            # memory — the shared bytes are read exactly once.
            for follower in followers:
                link_success(follower, quick, hashed)
                counts["reused"] += 1
            if counts["hashed"] % IDENTITY_BATCH_SIZE == 0:
                database.connect().commit()
                if job_id:
                    update_job(
                        database,
                        job_id,
                        processed_count=counts["hashed"] + counts["reused"],
                        current_item=f"{progress_phase} · {entry['absolute_path']}",
                    )
        except OSError:
            counts["errors"] += 1
    if record_throughput:
        record_identity_throughput(database, counts["bytes"], time.perf_counter() - started)
    database.connect().commit()
    if orphaned_followers:
        # Re-run the stranded followers on their own paths. hardlink_reuse=False so each is hashed
        # standalone (no further grouping, so no second orphaning tier); throughput is already
        # recorded above. Their hashed/reused/errors/bytes fold back into this run's totals.
        recovered = ensure_content_identity(
            database,
            config,
            orphaned_followers,
            job_id,
            workers=workers,
            record_errors=record_errors,
            record_throughput=False,
            hardlink_reuse=False,
            drop_cache=drop_cache,
            progress_phase=progress_phase,
        )
        for key in counts:
            counts[key] += recovered[key]
    return counts
