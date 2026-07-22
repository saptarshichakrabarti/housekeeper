"""Pure-Python FastCDC-style content-defined chunker (correctness reference backend).

A gear hash rolls over the byte stream; a boundary is cut when the fingerprint matches a mask.
Normalized chunking (two masks around the average size) keeps chunk sizes stable. Only one
chunk is buffered at a time, so memory stays bounded regardless of file size. This is the
deterministic reference; an optional acceleration backend can replace it later.
"""

from __future__ import annotations

import hashlib
import random
from pathlib import Path
from typing import Iterator

from .model import ChunkProfile, ChunkRecord

_MASK64 = (1 << 64) - 1


def _gear_table() -> list[int]:
    rng = random.Random(0xC0FFEE)  # fixed seed -> deterministic, reproducible chunking
    return [rng.getrandbits(64) for _ in range(256)]


_GEAR = _gear_table()


def chunk_file(path: Path, profile: ChunkProfile) -> Iterator[ChunkRecord]:
    minimum = profile.minimum_chunk_size
    average = max(2, profile.average_chunk_size)
    maximum = profile.maximum_chunk_size
    bits = average.bit_length() - 1
    mask_s = (1 << min(63, bits + 2)) - 1  # harder to cut below the average
    mask_l = (1 << max(1, bits - 2)) - 1  # easier to cut above the average
    fingerprint = 0
    buffer = bytearray()
    offset = 0
    sequence = 0
    with path.open("rb") as handle:
        while True:
            block = handle.read(1 << 20)
            if not block:
                break
            for byte in block:
                buffer.append(byte)
                fingerprint = ((fingerprint << 1) + _GEAR[byte]) & _MASK64
                size = len(buffer)
                if size < minimum:
                    continue
                cut = (
                    size >= maximum
                    or (size <= average and (fingerprint & mask_s) == 0)
                    or (size > average and (fingerprint & mask_l) == 0)
                )
                if cut:
                    yield ChunkRecord(
                        sequence, offset, size, hashlib.sha256(bytes(buffer)).hexdigest()
                    )
                    offset += size
                    sequence += 1
                    buffer = bytearray()
                    fingerprint = 0
    if buffer:
        yield ChunkRecord(
            sequence, offset, len(buffer), hashlib.sha256(bytes(buffer)).hexdigest()
        )
