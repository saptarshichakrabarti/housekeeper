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
            if any(
                n.endswith(x)
                for x in (".tar", ".tar.gz", ".tgz", ".tar.bz2", ".tar.xz")
            )
            else None
        )
    )


def normalize_archive_member_path(value: str) -> str:
    return value.replace("\\", "/").lstrip("/")


def inspect_archive(path: Path, config):
    try:
        if detect_archive_kind(path) == "zip":
            with zipfile.ZipFile(path) as z:
                ms = z.infolist()[: config.section("archives")["max_members"]]
                names = [normalize_archive_member_path(x.filename) for x in ms]
        else:
            with tarfile.open(path) as t:
                ms = t.getmembers()[: config.section("archives")["max_members"]]
                names = [normalize_archive_member_path(x.name) for x in ms]
        return {
            "archive_kind": detect_archive_kind(path),
            "member_count": len(names),
            "manifest_hash": hashlib.sha256("\n".join(names).encode()).hexdigest(),
            "analysis_status": "OK",
        }
    except (OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
        return {"analysis_status": "ERROR", "analysis_error": str(exc)}


def run_archive_analysis(database, config):
    return None
