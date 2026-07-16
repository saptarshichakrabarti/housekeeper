import shutil
from pathlib import Path
from typing import Any

from ..hashing import compute_full_hash, compute_quick_hash


class PythonBackend:
    def capabilities(self):
        return {"backend": "python", "protocol_version": "1", "operations": ["capabilities", "scan", "quick_hash", "full_hash", "aggregate_directories", "verify_manifest", "copy_and_verify"]}

    def full_hash(self, path: str, algorithm: str = "sha256", block_size: int = 8_388_608):
        result = compute_full_hash(Path(path), algorithm, block_size)
        return {
            "status": "ok" if result.stable else "error",
            "full_hash": result.digest,
            "size_bytes": result.size,
            "stable": result.stable,
            "error": result.error,
        }

    def quick_hash(self, path: str, algorithm: str = "sha256", chunk_size: int = 1_048_576, middle_samples: int = 2):
        result = compute_quick_hash(Path(path), chunk_size, middle_samples, algorithm)
        return {"status": "ok" if result.stable else "error", "quick_hash": result.digest, "size_bytes": result.size, "stable": result.stable, "error": result.error}

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
            result = compute_full_hash(Path(str(entry["path"])), "sha256", 8_388_608)
            valid = bool(result.stable and result.digest == entry.get("expected_hash") and result.size == entry.get("expected_size"))
            results.append({"path": entry["path"], "valid": valid, "error": result.error})
        return {"status": "ok", "valid": all(item["valid"] for item in results), "entries": results}

    def copy_and_verify(self, source: str, destination: str, expected_hash: str) -> dict[str, Any]:
        src, dst = Path(source), Path(destination)
        if dst.exists():
            return {"status": "error", "error": "destination exists"}
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            result = compute_full_hash(dst, "sha256", 8_388_608)
            if not result.stable or result.digest != expected_hash:
                dst.unlink(missing_ok=True)
                return {"status": "error", "error": "destination verification failed"}
            return {"status": "ok", "size_bytes": result.size, "full_hash": result.digest}
        except OSError as exc:
            return {"status": "error", "error": str(exc)}
