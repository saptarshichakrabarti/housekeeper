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
    """Hidden iff some component of *this* path starts with a dot.

    Callers must pass the path **relative to the source root**. On an absolute path a single dotted
    ancestor — ``/Volumes/.Backup``, ``~/.local/share`` — marks an entire drive hidden.
    """
    return any(part.startswith(".") for part in path.parts if part not in (".", ".."))


# The largest code point, so any string starting with ``prefix`` sorts before ``prefix + this``
# under SQLite's default BINARY collation.
_ABOVE_ALL_PATHS = "\U0010ffff"


def descendant_path_range(prefix_path: str) -> tuple[str, str]:
    """Half-open ``[low, high)`` bounds selecting everything beneath ``prefix_path``.

    ``relative_path LIKE ? || '/%'`` cannot use an index on the path column, so a descendant sweep
    degrades to a full table scan per directory — O(directories x entries). The equivalent explicit
    range does use the index. It is also stricter: SQLite's ``LIKE`` is ASCII case-insensitive by
    default, so the old predicate matched ``Photos/`` against ``photos/x`` as well.
    """
    if not prefix_path:
        return "", _ABOVE_ALL_PATHS  # the source root itself: every entry is a descendant
    prefix = prefix_path.rstrip("/") + "/"
    return prefix, prefix + _ABOVE_ALL_PATHS


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
