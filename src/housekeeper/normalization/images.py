"""Image normalization: decoded-pixel and orientation-normalized fingerprints.

A pixel-identical match means the *decoded* pixels are identical even though the raw bytes
differ (re-encoding, metadata, EXIF). It is stronger than a perceptual hash but weaker than
byte identity. Richer EXIF metadata is never discarded silently — its presence is recorded.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .model import NormalizationProfile, NormalizedArtifact

IMAGE_PIXEL_PROFILE = NormalizationProfile(
    name="IMAGE_PIXEL_EQUIVALENCE",
    content_kind="image",
    algorithm="decoded_pixel_hash",
    algorithm_version="1",
    configuration={},
    loss_characteristics=(
        "container_encoding",
        "lossless_recompression",
        "metadata",
        "exif",
        "icc_profile",
    ),
)


def normalize_image(path: Path, config) -> NormalizedArtifact:
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:
        return NormalizedArtifact("UNSUPPORTED", error_code="ImportError", error_message=str(exc.name))
    try:
        with Image.open(path) as image:
            if image.width * image.height > config.section("images")["max_pixels"]:
                return NormalizedArtifact("ERROR", error_code="pixel_limit", error_message="pixel limit")
            exif_present = bool(getattr(image, "_getexif", lambda: None)())
            base = image.convert("RGBA")
            pixel_hash = hashlib.sha256(
                f"{base.mode}:{base.size}:".encode() + base.tobytes()
            ).hexdigest()
            # exif_transpose returns None when there is nothing to transpose; fall back to the
            # original image so the orientation hash is always computable.
            oriented = (ImageOps.exif_transpose(image) or image).convert("RGBA")
            orientation_hash = hashlib.sha256(
                f"{oriented.mode}:{oriented.size}:".encode() + oriented.tobytes()
            ).hexdigest()
            return NormalizedArtifact(
                status="OK",
                normalized_hash=pixel_hash,
                normalized_size_bytes=len(base.tobytes()),
                structural_fingerprint=orientation_hash,
                artifact={
                    "orientation_normalized_hash": orientation_hash,
                    "width": image.width,
                    "height": image.height,
                    "mode": image.mode,
                    "exif_present": exif_present,
                },
            )
    except Exception as exc:  # noqa: BLE001 - a malformed image is isolated and recorded
        return NormalizedArtifact("ERROR", error_code=type(exc).__name__, error_message=str(exc))
