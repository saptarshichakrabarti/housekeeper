"""Content hashing: full digest, quick samples, and page-cache-aware opens.

``O_NOATIME`` / ``posix_fadvise`` reduce cache pollution on large scans. BLAKE3 is the default for
new workspaces; SHA-256 and SHA-512 remain supported so a workspace inventoried under them keeps
comparing against its own digests.
"""

import hashlib
import os
import threading
import time
from pathlib import Path
from typing import BinaryIO

from .constants import LEGACY_HASH_ALGORITHM
from .core import counters
from .models import HashResult

#: These are optimisations, not requirements — a platform without them still hashes correctly.
_HAS_FADVISE = hasattr(os, "posix_fadvise")
_O_NOATIME = getattr(os, "O_NOATIME", 0)


def new_hasher(algorithm: str):
    """A hasher object with the ``update``/``hexdigest`` protocol, for hashlib names or ``blake3``.

    ``content_objects`` is keyed by ``(hash_algorithm, full_hash, size_bytes)``, so a workspace can
    hold digests from more than one algorithm; cross-algorithm comparison (``coverage``) treats a
    hash mismatch as *unknown*, never covered, which is the safe direction.
    """
    if algorithm.lower() == "blake3":
        import blake3  # type: ignore[import-not-found]

        return blake3.blake3()
    return hashlib.new(algorithm)


def same_hash_algorithm(recorded: str | None, declared: str) -> bool:
    """Whether two algorithm names refer to the same function.

    A missing name predates the field rather than meaning "any": those digests are SHA-256 by
    definition, so treating the absence as a wildcard would let a BLAKE3 manifest be checked
    against a SHA-256 digest and fail for the wrong reason.
    """
    return (recorded or LEGACY_HASH_ALGORITHM).lower() == (declared or LEGACY_HASH_ALGORITHM).lower()


def workspace_hash_algorithm(database, configured: str) -> str:
    """Resolve and persist the single raw-content hash algorithm for a workspace.

    ``auto`` preserves a populated workspace's recorded algorithm and chooses BLAKE3 for a new
    workspace. A concrete configured value is an assertion; it may initialize an empty workspace,
    but cannot silently reinterpret existing digests. Legacy unnamed digests are SHA-256.
    """
    requested = str(configured).lower()
    supported = {"blake3", "sha256", "sha512", "blake2b"}
    if requested not in supported | {"auto"}:
        raise ValueError(f"unsupported hash algorithm: {configured}")

    # Inspect both stores of raw identity. UNION deduplicates the names; a NULL legacy signature
    # carrying a digest is SHA-256 by definition. Quick-only signatures matter too because rename
    # matching compares them across scans using the same algorithm.
    observed = {
        str(row["algorithm"]).lower()
        for row in database.fetch_all(
            """SELECT COALESCE(hash_algorithm, ?) AS algorithm FROM file_signatures
               WHERE full_hash IS NOT NULL OR quick_hash IS NOT NULL
               UNION
               SELECT lower(hash_algorithm) AS algorithm FROM content_objects""",
            (LEGACY_HASH_ALGORITHM,),
        )
        if row["algorithm"]
    }
    unknown = observed - supported
    if unknown:
        raise ValueError(f"workspace contains unsupported hash algorithm(s): {sorted(unknown)}")
    if len(observed) > 1:
        raise ValueError(
            "workspace contains mixed hash algorithms "
            f"({', '.join(sorted(observed))}); an explicit re-hash migration is required"
        )
    recorded = next(iter(observed), None)
    setting = database.fetch_one(
        "SELECT setting_value FROM workspace_settings WHERE setting_key='hash_algorithm'"
    )
    persisted = str(setting["setting_value"]).lower() if setting else None
    if persisted and persisted not in supported:
        raise ValueError(f"workspace has unsupported persisted hash algorithm: {persisted}")
    if persisted and recorded and persisted != recorded:
        raise ValueError(
            f"persisted workspace hash algorithm {persisted} conflicts with recorded {recorded}; "
            "an explicit re-hash migration is required"
        )

    effective = persisted or recorded or ("blake3" if requested == "auto" else requested)
    if requested != "auto" and requested != effective:
        raise ValueError(
            f"configured hash algorithm {requested} conflicts with workspace algorithm {effective}; "
            "an explicit re-hash migration is required"
        )
    if not persisted:
        database.execute(
            "INSERT INTO workspace_settings(setting_key,setting_value) VALUES('hash_algorithm',?)",
            (effective,),
        )
        database.connect().commit()
    return effective


def _open_for_hash(path: Path, sequential: bool = True) -> BinaryIO:
    """Open a file for hashing with reduced page-cache impact.

    ``O_NOATIME`` avoids an access-time write per file on an otherwise read-only scan; it is
    owner-only, so a file we do not own falls back to a plain open. ``posix_fadvise`` tells the
    kernel the access pattern — sequential for a full read, random for the sampled quick hash — so
    reading a terabyte through the cache once does not evict everything else the machine needs.
    """
    try:
        fd = os.open(path, os.O_RDONLY | _O_NOATIME)
    except PermissionError:
        fd = os.open(path, os.O_RDONLY)  # O_NOATIME requires ownership of the file
    if _HAS_FADVISE:
        try:
            os.posix_fadvise(  # type: ignore[attr-defined]
                fd,
                0,
                0,
                os.POSIX_FADV_SEQUENTIAL  # type: ignore[attr-defined]
                if sequential
                else os.POSIX_FADV_RANDOM,  # type: ignore[attr-defined]
            )
        except OSError:
            pass
    return os.fdopen(fd, "rb")


def _drop_from_cache(handle: BinaryIO) -> None:
    """Evict this file's pages after hashing it — for data nothing else is about to re-read.

    Used only for content that will not be parsed (too large, or a suffix no analyser opens): a
    long streaming scan of such files would otherwise fill the page cache with bytes read exactly
    once, at the expense of the database pages and directory metadata the run genuinely reuses.
    """
    if not _HAS_FADVISE:
        return
    try:
        os.posix_fadvise(  # type: ignore[attr-defined]
            handle.fileno(), 0, 0, os.POSIX_FADV_DONTNEED  # type: ignore[attr-defined]
        )
    except OSError:
        pass


def _quick_offsets(size: int, chunk_size: int, samples: int) -> list[int]:
    """The sampled read offsets, sorted and deduplicated.

    Unsorted they could seek backwards, and duplicates read the same bytes twice — on a spinning
    disk that is the difference between one pass and several.
    """
    return sorted(
        {0, max(0, size - chunk_size)}
        | {int(size * (i + 1) / (samples + 1)) for i in range(samples)}
    )


def _samples_whole_file(size: int, chunk_size: int, samples: int) -> bool:
    """True when sampling would read the whole file anyway — three times over, out of order."""
    return bool(size) and size <= (samples + 2) * chunk_size


def _hash(
    path: Path,
    algorithm: str,
    block_size: int,
    quick: bool = False,
    samples: int = 2,
    drop_cache: bool = False,
) -> HashResult:
    try:
        before = path.stat()
        h = new_hasher(algorithm)
        size = before.st_size
        read = 0
        # Read a small file once instead; the digest is then simply the full digest, which is
        # still a consistent quick-hash key for every file of that size.
        if quick and _samples_whole_file(size, block_size, samples):
            quick = False
        if quick and size:
            offsets = _quick_offsets(size, block_size, samples)
            with _open_for_hash(path, sequential=False) as f:
                for offset in offsets:
                    f.seek(offset)
                    chunk = f.read(block_size)
                    read += len(chunk)
                    h.update(chunk)
                if drop_cache:
                    _drop_from_cache(f)
        else:
            with _open_for_hash(path, sequential=True) as f:
                for chunk in iter(lambda: f.read(block_size), b""):
                    read += len(chunk)
                    h.update(chunk)
                if drop_cache:
                    _drop_from_cache(f)
        counters.count("quick_hash_bytes" if quick else "full_hash_bytes", read)
        counters.count("source_bytes_read", read)
        after = path.stat()
        stable = before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns
        return HashResult(
            h.hexdigest() if stable else None,
            after.st_size,
            stable,
            None if stable else "file changed during hashing",
        )
    except OSError as exc:
        return HashResult(None, 0, False, str(exc))


def _compute_quick_hash_python(
    path: Path, chunk_size: int, middle_samples: int, algorithm: str
) -> HashResult:
    return _hash(path, algorithm, chunk_size, True, middle_samples)


def _compute_full_hash_python(path: Path, algorithm: str, block_size: int) -> HashResult:
    return _hash(path, algorithm, block_size)


def bound_native_parallelism(workers: int) -> None:
    """Cap the native core's intra-file BLAKE3 threads against the file-level worker count.

    The identity stage hashes ``workers`` files at once, each in its own core process. If every one
    of those also spread a single file across all cores, the two layers would oversubscribe and run
    slower than either alone. Setting ``RAYON_NUM_THREADS = cores // workers`` keeps their product
    near the core count: a scan already saturating the cores with one file per core hashes each file
    single-threaded, while a source hashed by one worker (a few very large files) gets all cores per
    file. Core processes spawned after this inherit the value; rayon reads it when it builds its pool.
    """
    cpu = os.cpu_count() or 1
    os.environ["RAYON_NUM_THREADS"] = str(max(1, cpu // max(1, workers)))


_native_backends = threading.local()


def _native_backend():
    """Return this thread's compatible Rust backend, or ``None``.

    The subprocess protocol is request/response ordered, while identity hashing uses worker
    threads. Giving each worker its own process avoids interleaving requests, and caching the
    result means capability discovery is not repeated for every file.
    """
    if hasattr(_native_backends, "backend"):
        return _native_backends.backend
    try:
        from .acceleration.capability_detection import detect_backend

        backend = detect_backend()
        if backend.capabilities().get("backend") != "rust":
            backend = None
    except (OSError, RuntimeError, ValueError):
        backend = None
    _native_backends.backend = backend
    return backend


def _disable_native_backend(backend: object) -> None:
    close = getattr(backend, "close", None)
    if callable(close):
        close()
    _native_backends.backend = None


def _native_hash(
    operation: str, path: Path, algorithm: str, block_size: int, middle_samples: int = 2
) -> HashResult | None:
    """Hash through Rust, returning ``None`` when Python must take over."""
    backend = _native_backend()
    if backend is None:
        return None
    try:
        if operation == "full_hash":
            reply = backend.full_hash(str(path), algorithm, block_size)
            field = "full_hash"
        else:
            reply = backend.quick_hash(str(path), algorithm, block_size, middle_samples)
            field = "quick_hash"
        return HashResult(
            reply.get(field),
            int(reply.get("size_bytes", 0)),
            bool(reply.get("stable", False)),
            reply.get("error"),
        )
    except (OSError, RuntimeError, ValueError):
        # A native failure must never turn a scan or a safety verification into a failure merely
        # because acceleration is unavailable. Discard the process before using the reference.
        _disable_native_backend(backend)
        return None


def _native_identity(
    path: Path,
    algorithm: str,
    block_size: int,
    quick_chunk_bytes: int,
    quick_samples: int,
) -> tuple[HashResult, HashResult] | None:
    """Compute both digests through Rust's one-read identity operation."""
    backend = _native_backend()
    operation = getattr(backend, "identity_hash", None) if backend is not None else None
    if not callable(operation):
        return None
    try:
        reply = operation(
            str(path), algorithm, block_size, quick_chunk_bytes, quick_samples
        )
        size = int(reply.get("size_bytes", 0))
        stable = bool(reply.get("stable", False))
        error = reply.get("error")
        counters.count("full_hash_bytes", int(reply.get("bytes_read", size)))
        counters.count("source_bytes_read", int(reply.get("bytes_read", size)))
        return (
            HashResult(reply.get("full_hash"), size, stable, error),
            HashResult(reply.get("quick_hash"), size, stable, error),
        )
    except (OSError, RuntimeError, ValueError):
        _disable_native_backend(backend)
        return None


def compute_quick_hash(
    path: Path, chunk_size: int, middle_samples: int, algorithm: str
) -> HashResult:
    """Compute via Rust when available; otherwise use the reference Python implementation."""
    result = _native_hash("quick_hash", path, algorithm, chunk_size, middle_samples)
    return result if result is not None else _compute_quick_hash_python(
        path, chunk_size, middle_samples, algorithm
    )


def compute_full_hash(path: Path, algorithm: str, block_size: int) -> HashResult:
    """Compute via Rust when available; otherwise use the reference Python implementation."""
    result = _native_hash("full_hash", path, algorithm, block_size)
    return result if result is not None else _compute_full_hash_python(path, algorithm, block_size)


def _compute_identity_python(
    path: Path,
    algorithm: str,
    block_size: int,
    quick_chunk_bytes: int,
    quick_samples: int,
    drop_cache: bool = False,
) -> tuple[HashResult, HashResult]:
    """Both digests from one sequential read. Returns ``(full, quick)``.

    Quick samples are captured as the full-hash stream passes — no second pass. Digests match
    ``compute_quick_hash`` / ``compute_full_hash`` so older stored hashes still compare.
    ``drop_cache`` only when nothing will parse the file next. Memory bound:
    ``(quick_samples + 2) * quick_chunk_bytes``.
    """
    try:
        before = path.stat()
        size = before.st_size
        full = new_hasher(algorithm)
        if _samples_whole_file(size, quick_chunk_bytes, quick_samples):
            # The quick digest of a small file *is* its full digest, so there is nothing to sample.
            offsets: list[int] = []
        else:
            offsets = _quick_offsets(size, quick_chunk_bytes, quick_samples)
        captured: dict[int, bytearray] = {offset: bytearray() for offset in offsets}
        read = 0
        # Split read time from digest time — only while recording, so production pays nothing. This
        # is the measurement that decides whether a faster hash (BLAKE3) is worth adopting: on a
        # small-file corpus the digest is a few percent of the stage; on large files on fast storage
        # the balance can tip, and that is when the algorithm choice starts to matter.
        timing = counters.is_recording()
        io_ms = cpu_ms = 0.0
        with _open_for_hash(path, sequential=True) as handle:
            position = 0
            while True:
                mark = time.perf_counter() if timing else 0.0
                chunk = handle.read(block_size)
                if timing:
                    io_ms += (time.perf_counter() - mark) * 1000
                if not chunk:
                    break
                mark = time.perf_counter() if timing else 0.0
                full.update(chunk)
                for offset in offsets:
                    low = max(offset, position)
                    high = min(offset + quick_chunk_bytes, position + len(chunk))
                    if low < high:
                        captured[offset] += chunk[low - position : high - position]
                if timing:
                    cpu_ms += (time.perf_counter() - mark) * 1000
                position += len(chunk)
                read += len(chunk)
            if drop_cache:
                _drop_from_cache(handle)
        counters.count("full_hash_bytes", read)
        counters.count("source_bytes_read", read)
        if timing:
            counters.count("stage_ms:hash_io", int(io_ms))
            counters.count("stage_ms:hash_cpu", int(cpu_ms))
        after = path.stat()
        stable = before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns
        if not stable:
            unstable = HashResult(None, after.st_size, False, "file changed during hashing")
            return unstable, unstable
        full_result = HashResult(full.hexdigest(), after.st_size, True, None)
        if not offsets:
            return full_result, full_result
        quick = new_hasher(algorithm)
        for offset in offsets:
            quick.update(bytes(captured[offset]))
        return full_result, HashResult(quick.hexdigest(), after.st_size, True, None)
    except OSError as exc:
        failed = HashResult(None, 0, False, str(exc))
        return failed, failed


def compute_identity(
    path: Path,
    algorithm: str,
    block_size: int,
    quick_chunk_bytes: int,
    quick_samples: int,
    drop_cache: bool = False,
) -> tuple[HashResult, HashResult]:
    """Compute full and quick digests, preferring the Rust backend when installed.

    Rust computes both results in one sequential read. If that operation is unavailable or fails,
    Python recomputes the pair through its equivalent one-read reference implementation.
    """
    # The reference implementation is also the instrumentation and cache-control path. Rust's
    # protocol intentionally reports only stable digest results, so retain those Python-only
    # semantics whenever a caller explicitly asks for them.
    if drop_cache or counters.is_recording():
        return _compute_identity_python(
            path, algorithm, block_size, quick_chunk_bytes, quick_samples, drop_cache
        )
    native = _native_identity(path, algorithm, block_size, quick_chunk_bytes, quick_samples)
    if native is not None:
        return native
    return _compute_identity_python(
        path, algorithm, block_size, quick_chunk_bytes, quick_samples, drop_cache
    )


def verify_file_against_manifest(
    path: Path, expected_size: int, expected_hash: str, algorithm: str
) -> HashResult:
    """Re-hash ``path`` and **raise** unless it still matches the manifest exactly.

    A verifier that returns the same value on match and mismatch is worse than no verifier: it
    reads as covered. The contract is therefore "returns or raises", so a caller cannot ignore the
    answer by accident. This is the single pre-move check; ``review_mover`` calls it.

    ``algorithm`` is the manifest's own declared algorithm, not a fixed one: verifying a BLAKE3
    manifest with SHA-256 would fail every entry, and verifying with whatever the config happens to
    say today would compare a digest against a digest of a different function.
    """
    result = compute_full_hash(path, algorithm, 8_388_608)
    if not result.stable:
        raise ValueError(f"{path}: changed while hashing ({result.error})")
    if result.size != expected_size or result.digest != expected_hash:
        raise ValueError(f"{path}: pre-move hash mismatch")
    return result
