"""Chunking value objects and the backend protocol."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Protocol


@dataclass(frozen=True)
class ChunkProfile:
    name: str
    algorithm: str
    algorithm_version: str
    minimum_chunk_size: int
    average_chunk_size: int
    maximum_chunk_size: int
    hash_algorithm: str = "sha256"

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(
                {
                    "algorithm": self.algorithm,
                    "version": self.algorithm_version,
                    "min": self.minimum_chunk_size,
                    "avg": self.average_chunk_size,
                    "max": self.maximum_chunk_size,
                    "hash": self.hash_algorithm,
                },
                sort_keys=True,
            ).encode()
        ).hexdigest()


@dataclass(frozen=True)
class ChunkRecord:
    sequence_index: int
    byte_offset: int
    size_bytes: int
    chunk_hash: str


class ChunkingBackend(Protocol):
    def chunk_file(self, path: Path, profile: ChunkProfile) -> Iterator[ChunkRecord]:
        ...
