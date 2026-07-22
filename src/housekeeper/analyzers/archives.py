import hashlib
import tarfile
import zipfile
from pathlib import Path


def detect_archive_kind(path: Path):
    n = path.name.lower()
    return (
        "zip"
        if n.endswith(".zip")
        else (
            "tar"
            if any(n.endswith(x) for x in (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz"))
            else None
        )
    )


def normalize_archive_member_path(value: str) -> str:
    return value.replace("\\", "/").lstrip("/")


def _safe_member_name(value: str) -> bool:
    parts = normalize_archive_member_path(value).split("/")
    return ".." not in parts and all(part not in {"", "."} for part in parts if part)


def inspect_archive(path: Path, config):
    try:
        names: list[str]
        max_members = config.section("archives")["max_members"]
        if detect_archive_kind(path) == "zip":
            with zipfile.ZipFile(path) as z:
                zip_members = z.infolist()
                if len(zip_members) > max_members:
                    return {
                        "analysis_status": "ERROR",
                        "analysis_error": f"archive exceeds max_members ({len(zip_members)}>{max_members})",
                    }
                if (
                    sum(max(0, item.file_size) for item in zip_members)
                    > config.section("archives")["max_declared_uncompressed_bytes"]
                ):
                    return {
                        "analysis_status": "ERROR",
                        "analysis_error": "declared archive size limit",
                    }
                if any(not _safe_member_name(item.filename) for item in zip_members):
                    return {
                        "analysis_status": "ERROR",
                        "analysis_error": "unsafe archive member path",
                    }
                names = [normalize_archive_member_path(x.filename) for x in zip_members]
        else:
            with tarfile.open(path) as t:
                tar_members = t.getmembers()
                if len(tar_members) > max_members:
                    return {
                        "analysis_status": "ERROR",
                        "analysis_error": f"archive exceeds max_members ({len(tar_members)}>{max_members})",
                    }
                if (
                    sum(max(0, item.size) for item in tar_members)
                    > config.section("archives")["max_declared_uncompressed_bytes"]
                ):
                    return {
                        "analysis_status": "ERROR",
                        "analysis_error": "declared archive size limit",
                    }
                if any(not _safe_member_name(item.name) for item in tar_members):
                    return {
                        "analysis_status": "ERROR",
                        "analysis_error": "unsafe archive member path",
                    }
                names = [normalize_archive_member_path(x.name) for x in tar_members]
        return {
            "archive_kind": detect_archive_kind(path),
            "member_count": len(names),
            "manifest_hash": hashlib.sha256("\n".join(names).encode()).hexdigest(),
            # Nested archives are reported as inventory only.  This analyzer never opens
            # them recursively, avoiding decompression bombs and path traversal chains.
            "nested_archive_count": sum(
                1
                for name in names
                if name.lower().endswith((".zip", ".tar", ".tgz", ".tar.gz", ".gz", ".bz2", ".xz"))
            ),
            "nested_analysis": "NOT_EXPANDED",
            "analysis_status": "OK",
        }
    except (OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
        return {"analysis_status": "ERROR", "analysis_error": str(exc)}


def run_archive_analysis(database, config, scope=None, job_id=None):
    from .registry import run_content_analysis

    return run_content_analysis(database, config, "archives", job_id=job_id)
