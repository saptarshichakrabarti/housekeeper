"""Value objects for the normalization layer."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass(frozen=True)
class NormalizationProfile:
    """A deterministic, versioned normalization definition.

    ``loss_characteristics`` names every kind of information the profile removes (timestamps,
    metadata, compression, XML ordering, EXIF, container tags, ...). It is stored so a match on
    the normalized hash can always be explained in terms of what was ignored.
    """

    name: str
    content_kind: str
    algorithm: str
    algorithm_version: str
    configuration: dict[str, Any] = field(default_factory=dict)
    loss_characteristics: tuple[str, ...] = ()

    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(
                {"algorithm": self.algorithm, "version": self.algorithm_version, "config": self.configuration},
                sort_keys=True,
            ).encode()
        ).hexdigest()


@dataclass(frozen=True)
class NormalizedArtifact:
    """The result of normalizing one content object under one profile."""

    status: str  # OK | ERROR | UNSUPPORTED
    normalized_hash: str | None = None
    normalized_size_bytes: int | None = None
    structural_fingerprint: str | None = None
    artifact: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
