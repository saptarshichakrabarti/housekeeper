"""Content-defined chunking dispatch: the native core when available, the Python reference otherwise.

Mirrors ``housekeeper.hashing``: the Rust ``housekeeper-core`` is byte-identical to
``python_backend.chunk_file`` (guarded by ``tests/test_acceleration.py``), so preferring it changes
throughput, never the stored chunk boundaries or digests. Any native failure falls back to Python —
acceleration is never a correctness requirement.
"""

from __future__ import annotations

from pathlib import Path

from .model import ChunkProfile, ChunkRecord
from .python_backend import chunk_file as _python_chunk_file


def chunk_file(path: Path, profile: ChunkProfile) -> list[ChunkRecord]:
    """Chunk ``path`` under ``profile``, preferring the native core, returning the chunk sequence."""
    native = _native_chunk_file(path, profile)
    if native is not None:
        return native
    return list(_python_chunk_file(path, profile))


def _native_chunk_file(path: Path, profile: ChunkProfile) -> list[ChunkRecord] | None:
    """Chunk through the Rust core, or ``None`` when Python must take over.

    Reuses the same per-thread native backend the hashing path detects, so capability discovery and
    the process are shared rather than re-established here.
    """
    from ..hashing import _disable_native_backend, _native_backend

    backend = _native_backend()
    operation = getattr(backend, "chunk_file", None) if backend is not None else None
    if not callable(operation):
        return None
    try:
        reply = operation(
            str(path),
            profile.minimum_chunk_size,
            profile.average_chunk_size,
            profile.maximum_chunk_size,
            profile.hash_algorithm,
        )
    except (OSError, RuntimeError, ValueError):
        # A native failure must never fail the stage merely because acceleration is unavailable.
        _disable_native_backend(backend)
        return None
    if reply.get("status") != "ok":
        return None
    return [
        ChunkRecord(
            int(record["sequence_index"]),
            int(record["byte_offset"]),
            int(record["size_bytes"]),
            record["chunk_hash"],
        )
        for record in reply.get("chunks", [])
    ]
