import hashlib
from pathlib import Path

from .core import counters
from .models import HashResult


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
    path: Path, algorithm: str, block_size: int, quick: bool = False, samples: int = 2
) -> HashResult:
    try:
        before = path.stat()
        h = hashlib.new(algorithm)
        size = before.st_size
        read = 0
        # Read a small file once instead; the digest is then simply the full digest, which is
        # still a consistent quick-hash key for every file of that size.
        if quick and _samples_whole_file(size, block_size, samples):
            quick = False
        if quick and size:
            offsets = _quick_offsets(size, block_size, samples)
            with path.open("rb") as f:
                for offset in offsets:
                    f.seek(offset)
                    chunk = f.read(block_size)
                    read += len(chunk)
                    h.update(chunk)
        else:
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(block_size), b""):
                    read += len(chunk)
                    h.update(chunk)
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


def compute_quick_hash(
    path: Path, chunk_size: int, middle_samples: int, algorithm: str
) -> HashResult:
    return _hash(path, algorithm, chunk_size, True, middle_samples)


def compute_full_hash(path: Path, algorithm: str, block_size: int) -> HashResult:
    return _hash(path, algorithm, block_size)


def compute_identity(
    path: Path, algorithm: str, block_size: int, quick_chunk_bytes: int, quick_samples: int
) -> tuple[HashResult, HashResult]:
    """Both digests for one file from **one** sequential read. Returns ``(full, quick)``.

    Establishing identity used to read the same file twice — a quick hash that seeks to several
    offsets, then a full hash over everything — for a quick digest that is, by construction, a
    subset of the bytes the full hash already went past. Here the sampled ranges are captured as
    the stream flows by, so the quick digest is a by-product rather than a second pass. The
    digests are byte-identical to what :func:`compute_quick_hash` and :func:`compute_full_hash`
    produce, so hashes stored by earlier versions still compare.

    Memory is bounded by ``(quick_samples + 2) * quick_chunk_bytes`` — 4 MB at the defaults.
    """
    try:
        before = path.stat()
        size = before.st_size
        full = hashlib.new(algorithm)
        if _samples_whole_file(size, quick_chunk_bytes, quick_samples):
            # The quick digest of a small file *is* its full digest, so there is nothing to sample.
            offsets: list[int] = []
        else:
            offsets = _quick_offsets(size, quick_chunk_bytes, quick_samples)
        captured: dict[int, bytearray] = {offset: bytearray() for offset in offsets}
        read = 0
        with path.open("rb") as handle:
            position = 0
            while chunk := handle.read(block_size):
                full.update(chunk)
                for offset in offsets:
                    low = max(offset, position)
                    high = min(offset + quick_chunk_bytes, position + len(chunk))
                    if low < high:
                        captured[offset] += chunk[low - position : high - position]
                position += len(chunk)
                read += len(chunk)
        counters.count("full_hash_bytes", read)
        counters.count("source_bytes_read", read)
        after = path.stat()
        stable = before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns
        if not stable:
            unstable = HashResult(None, after.st_size, False, "file changed during hashing")
            return unstable, unstable
        full_result = HashResult(full.hexdigest(), after.st_size, True, None)
        if not offsets:
            return full_result, full_result
        quick = hashlib.new(algorithm)
        for offset in offsets:
            quick.update(bytes(captured[offset]))
        return full_result, HashResult(quick.hexdigest(), after.st_size, True, None)
    except OSError as exc:
        failed = HashResult(None, 0, False, str(exc))
        return failed, failed


def verify_file_against_manifest(path: Path, expected_size: int, expected_hash: str) -> HashResult:
    """Re-hash ``path`` and **raise** unless it still matches the manifest exactly.

    A verifier that returns the same value on match and mismatch is worse than no verifier: it
    reads as covered. The contract is therefore "returns or raises", so a caller cannot ignore the
    answer by accident. This is the single pre-move check; ``review_mover`` calls it.
    """
    result = compute_full_hash(path, "sha256", 8_388_608)
    if not result.stable:
        raise ValueError(f"{path}: changed while hashing ({result.error})")
    if result.size != expected_size or result.digest != expected_hash:
        raise ValueError(f"{path}: pre-move hash mismatch")
    return result
