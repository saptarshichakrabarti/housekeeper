"""PDF normalization: ordered page-text fingerprint.

Detects PDFs with identical extracted text even after re-encoding. This is a *text* equivalence
(Tier 3), never a claim of visual/rendering equivalence. PDFs with no extractable text (e.g.
pure scans) are reported UNSUPPORTED rather than matching each other on empty text.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

from .model import NormalizationProfile, NormalizedArtifact

PDF_TEXT_PROFILE = NormalizationProfile(
    name="PDF_TEXT_EQUIVALENCE",
    content_kind="pdf",
    algorithm="pdf_ordered_page_text_hash",
    algorithm_version="1",
    configuration={},
    loss_characteristics=(
        "encoding",
        "compression",
        "metadata",
        "page_geometry",
        "embedded_fonts",
        "visual_layout",
    ),
)

_MIN_TEXT_CHARS = 32


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


def normalize_pdf(path: Path, config) -> NormalizedArtifact:
    try:
        import fitz  # type: ignore[import-untyped, import-not-found]
    except ImportError as exc:
        return NormalizedArtifact("UNSUPPORTED", error_code="ImportError", error_message=str(exc.name))
    try:
        document = fitz.open(path)
        page_hashes = []
        total_chars = 0
        for page in document:
            text = _normalize(page.get_text("text"))
            total_chars += len(text)
            page_hashes.append(hashlib.sha256(text.encode()).hexdigest())
        page_count = len(page_hashes)
        document.close()
        if total_chars < _MIN_TEXT_CHARS:
            return NormalizedArtifact("UNSUPPORTED", error_code="insufficient_text")
        manifest = "\n".join(page_hashes)
        return NormalizedArtifact(
            status="OK",
            normalized_hash=hashlib.sha256(manifest.encode()).hexdigest(),
            normalized_size_bytes=total_chars,
            structural_fingerprint=hashlib.sha256(str(page_count).encode()).hexdigest(),
            artifact={"page_count": page_count, "text_chars": total_chars},
        )
    except Exception as exc:  # noqa: BLE001 - malformed PDF isolated and recorded
        return NormalizedArtifact("ERROR", error_code=type(exc).__name__, error_message=str(exc))
