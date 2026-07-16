import hashlib
from pathlib import Path

from .models import HashResult


def _hash(
    path: Path, algorithm: str, block_size: int, quick: bool = False, samples: int = 2
) -> HashResult:
    try:
        before = path.stat()
        h = hashlib.new(algorithm)
        size = before.st_size
        if quick and size:
            offsets = [0, max(0, size - block_size)] + [
                int(size * (i + 1) / (samples + 1)) for i in range(samples)
            ]
            with path.open("rb") as f:
                for offset in offsets:
                    f.seek(offset)
                    h.update(f.read(block_size))
        else:
            with path.open("rb") as f:
                for chunk in iter(lambda: f.read(block_size), b""):
                    h.update(chunk)
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


def compare_files_bytewise(path_a: Path, path_b: Path, block_size: int = 8_388_608) -> bool:
    try:
        with path_a.open("rb") as a, path_b.open("rb") as b:
            while True:
                x, y = a.read(block_size), b.read(block_size)
                if not x and not y:
                    break
                if x != y:
                    return False
        return True
    except OSError:
        return False


def verify_file_against_manifest(path: Path, expected_size: int, expected_hash: str) -> HashResult:
    result = compute_full_hash(path, "sha256", 8_388_608)
    return result if result.size != expected_size or result.digest != expected_hash else result
