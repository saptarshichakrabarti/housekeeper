import os
import re
from pathlib import Path


def normalize_absolute_path(path: Path) -> Path:
    return Path(os.path.abspath(os.path.normpath(os.fspath(path))))


def safe_relative_path(path: Path, root: Path) -> Path:
    p, r = normalize_absolute_path(path), normalize_absolute_path(root)
    try:
        return p.relative_to(r)
    except ValueError as exc:
        raise ValueError(f"path is outside root: {p}") from exc


def is_within(path: Path, root: Path) -> bool:
    try:
        safe_relative_path(path, root)
        return True
    except ValueError:
        return False


def is_same_filesystem_object(path_a: Path, path_b: Path) -> bool:
    try:
        return os.path.samestat(
            os.stat(path_a, follow_symlinks=False),
            os.stat(path_b, follow_symlinks=False),
        )
    except OSError:
        return False


def is_hidden_path(path: Path) -> bool:
    return any(part.startswith(".") for part in path.parts if part not in (".", ".."))


def sanitize_report_filename(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)[:200] or "report"


def safe_destination_path(review_root: Path, relative_path: Path) -> Path:
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ValueError("unsafe relative path")
    result = normalize_absolute_path(review_root / relative_path)
    if not is_within(result, review_root):
        raise ValueError("destination escapes review root")
    return result


def detect_case_sensitivity(root: Path) -> bool:
    # Conservative platform default; callers may override after probing.
    return os.name != "nt"
