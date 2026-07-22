"""Audio normalization: MP3 audio-payload fingerprint (tags stripped).

Detects files with the same audio stream but different ID3 tags. The normalized hash is the
audio payload with ID3v2/ID3v1 tag regions removed, so a tag edit does not change it. This is a
*payload* match, never a claim of acoustic identity across different encodings.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .model import NormalizationProfile, NormalizedArtifact

AUDIO_PAYLOAD_PROFILE = NormalizationProfile(
    name="AUDIO_PAYLOAD_EQUIVALENCE",
    content_kind="audio",
    algorithm="mp3_payload_hash",
    algorithm_version="1",
    configuration={},
    loss_characteristics=("id3v1_tags", "id3v2_tags", "container_metadata"),
)


def _id3v2_size(header: bytes) -> int:
    if len(header) < 10 or header[:3] != b"ID3":
        return 0
    # 28-bit syncsafe integer in bytes 6..9, plus the 10-byte header.
    size = 0
    for byte in header[6:10]:
        size = (size << 7) | (byte & 0x7F)
    return size + 10


def normalize_audio(path: Path, config) -> NormalizedArtifact:
    if path.suffix.lower() != ".mp3":
        return NormalizedArtifact("UNSUPPORTED", error_code="not_mp3")
    try:
        with path.open("rb") as handle:
            head = handle.read(10)
            start = _id3v2_size(head)
            handle.seek(0, 2)
            end = handle.tell()
            # ID3v1 trailer is a fixed 128-byte block starting with "TAG".
            if end - start >= 128:
                handle.seek(end - 128)
                if handle.read(3) == b"TAG":
                    end -= 128
            if end <= start:
                return NormalizedArtifact("UNSUPPORTED", error_code="no_payload")
            handle.seek(start)
            digest = hashlib.sha256()
            remaining = end - start
            while remaining > 0:
                chunk = handle.read(min(1 << 20, remaining))
                if not chunk:
                    break
                digest.update(chunk)
                remaining -= len(chunk)
        return NormalizedArtifact(
            status="OK",
            normalized_hash=digest.hexdigest(),
            normalized_size_bytes=end - start,
            structural_fingerprint=digest.hexdigest(),
            artifact={"payload_bytes": end - start, "id3v2_bytes": start},
        )
    except OSError as exc:
        return NormalizedArtifact("ERROR", error_code=type(exc).__name__, error_message=str(exc))
