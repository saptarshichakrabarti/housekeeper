"""Optional binary fuzzy-hash capability layer (TLSH / ssdeep).

Core install must not require them. Digests/distances when available; candidates only —
never an exact classification.
"""

from __future__ import annotations

from pathlib import Path


def capabilities() -> dict[str, bool]:
    tlsh_available = False
    ssdeep_available = False
    try:
        import tlsh  # type: ignore[import-not-found]  # noqa: F401

        tlsh_available = True
    except ImportError:
        pass
    try:
        import ssdeep  # type: ignore[import-not-found]  # noqa: F401

        ssdeep_available = True
    except ImportError:
        pass
    return {"TLSH_AVAILABLE": tlsh_available, "SSDEEP_AVAILABLE": ssdeep_available}


def tlsh_digest(path: Path) -> str | None:
    try:
        import tlsh  # type: ignore[import-not-found]

        digest = tlsh.hash(path.read_bytes())
        return digest if digest and digest != "TNULL" else None
    except (ImportError, ValueError, OSError):
        return None


def tlsh_distance(a: str, b: str) -> int | None:
    try:
        import tlsh  # type: ignore[import-not-found]

        return int(tlsh.diff(a, b))
    except (ImportError, ValueError):
        return None


def ssdeep_digest(path: Path) -> str | None:
    try:
        import ssdeep  # type: ignore[import-not-found]

        return str(ssdeep.hash(path.read_bytes()))
    except (ImportError, OSError):
        return None


def ssdeep_similarity(a: str, b: str) -> int | None:
    try:
        import ssdeep  # type: ignore[import-not-found]

        return int(ssdeep.compare(a, b))
    except (ImportError, ValueError):
        return None
