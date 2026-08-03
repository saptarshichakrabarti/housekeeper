import shutil
from pathlib import Path
from typing import Any

from ..constants import LEGACY_HASH_ALGORITHM
from ..hashing import (
    _compute_full_hash_python,
    _compute_identity_python,
    _compute_quick_hash_python,
)


class PythonBackend:
    def capabilities(self):
        return {"backend": "python", "protocol_version": "1", "operations": ["capabilities", "scan", "quick_hash", "full_hash", "identity_hash", "chunk_file", "aggregate_directories", "verify_manifest", "copy_and_verify"]}

    def chunk_file(
        self,
        path: str,
        minimum_chunk_size: int = 16_384,
        average_chunk_size: int = 65_536,
        maximum_chunk_size: int = 262_144,
        hash_algorithm: str = "sha256",
    ) -> dict[str, Any]:
        """Content-defined chunks for a file — the reference the Rust core must match byte for byte."""
        from ..chunking.model import ChunkProfile
        from ..chunking.python_backend import chunk_file as _chunk

        profile = ChunkProfile(
            "_", "fastcdc_gear", "1", minimum_chunk_size, average_chunk_size, maximum_chunk_size, hash_algorithm
        )
        chunks = [
            {
                "sequence_index": record.sequence_index,
                "byte_offset": record.byte_offset,
                "size_bytes": record.size_bytes,
                "chunk_hash": record.chunk_hash,
            }
            for record in _chunk(Path(path), profile)
        ]
        return {"status": "ok", "count": len(chunks), "chunks": chunks}

    def full_hash(self, path: str, algorithm: str = "sha256", block_size: int = 8_388_608):
        result = _compute_full_hash_python(Path(path), algorithm, block_size)
        return {
            "status": "ok" if result.stable else "error",
            "full_hash": result.digest,
            "size_bytes": result.size,
            "stable": result.stable,
            "error": result.error,
        }

    def quick_hash(self, path: str, algorithm: str = "sha256", chunk_size: int = 1_048_576, middle_samples: int = 2):
        result = _compute_quick_hash_python(Path(path), chunk_size, middle_samples, algorithm)
        return {"status": "ok" if result.stable else "error", "quick_hash": result.digest, "size_bytes": result.size, "stable": result.stable, "error": result.error}

    def identity_hash(
        self,
        path: str,
        algorithm: str = "blake3",
        block_size: int = 8_388_608,
        quick_chunk_size: int = 1_048_576,
        middle_samples: int = 2,
    ):
        full, quick = _compute_identity_python(
            Path(path), algorithm, block_size, quick_chunk_size, middle_samples
        )
        return {
            "status": "ok" if full.stable and quick.stable else "error",
            "full_hash": full.digest,
            "quick_hash": quick.digest,
            "size_bytes": full.size,
            "bytes_read": full.size,
            "stable": full.stable and quick.stable,
            "error": full.error or quick.error,
        }

    def scan(self, path: str) -> dict[str, Any]:
        root = Path(path)
        try:
            entries = [{"relative_path": str(item.relative_to(root)), "entry_type": "directory" if item.is_dir() else "file" if item.is_file() else "other", "size_bytes": item.stat(follow_symlinks=False).st_size} for item in root.iterdir() if not item.is_symlink()]
            return {"status": "ok", "entries": entries}
        except OSError as exc:
            return {"status": "error", "error": str(exc)}

    def aggregate_directories(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        totals: dict[str, dict[str, int]] = {}
        for entry in entries:
            top = str(entry.get("relative_path", "")).split("/", 1)[0]
            item = totals.setdefault(top, {"file_count": 0, "size_bytes": 0})
            item["file_count"] += int(entry.get("entry_type") == "file")
            item["size_bytes"] += int(entry.get("size_bytes", 0))
        return {"status": "ok", "directories": totals}

    def verify_manifest(self, entries: list[dict[str, Any]]) -> dict[str, Any]:
        results = []
        for entry in entries:
            # Each entry carries the function its expected_hash was produced with; an entry from a
            # manifest that predates the field is SHA-256.
            algorithm = str(entry.get("expected_hash_algorithm") or LEGACY_HASH_ALGORITHM)
            result = _compute_full_hash_python(Path(str(entry["path"])), algorithm, 8_388_608)
            valid = bool(result.stable and result.digest == entry.get("expected_hash") and result.size == entry.get("expected_size"))
            results.append({"path": entry["path"], "valid": valid, "error": result.error})
        return {"status": "ok", "valid": all(item["valid"] for item in results), "entries": results}

    def copy_and_verify(
        self, source: str, destination: str, expected_hash: str, algorithm: str = LEGACY_HASH_ALGORITHM
    ) -> dict[str, Any]:
        src, dst = Path(source), Path(destination)
        if dst.exists():
            return {"status": "error", "error": "destination exists"}
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            result = _compute_full_hash_python(dst, algorithm, 8_388_608)
            if not result.stable or result.digest != expected_hash:
                dst.unlink(missing_ok=True)
                return {"status": "error", "error": "destination verification failed"}
            return {"status": "ok", "size_bytes": result.size, "full_hash": result.digest}
        except OSError as exc:
            return {"status": "error", "error": str(exc)}
