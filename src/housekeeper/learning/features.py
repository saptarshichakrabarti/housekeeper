"""Deterministic, interpretable feature extraction for review learning.

Features are structural only — no raw private document text is ever used.
"""

from __future__ import annotations

import math

FEATURE_NAMES = [
    "is_review_safe",
    "is_review_probable",
    "is_protected",
    "is_error",
    "has_exact_duplicate",
    "requires_manual_approval",
    "confidence",
    "size_log10",
    "is_image",
    "is_document",
    "is_archive_or_installer",
]

_IMAGE = {".jpg", ".jpeg", ".png", ".gif", ".tiff", ".bmp", ".webp"}
_DOCUMENT = {".txt", ".md", ".csv", ".pdf", ".docx", ".xlsx", ".pptx"}
_ARCHIVE = {".zip", ".tar", ".gz", ".exe", ".msi", ".dmg", ".iso"}


def entry_features(row) -> list[float]:
    classification = row["classification"] or "KEEP"
    suffix = (row["suffix"] or "").lower()
    return [
        1.0 if classification == "REVIEW_SAFE" else 0.0,
        1.0 if classification == "REVIEW_PROBABLE" else 0.0,
        1.0 if classification == "PROTECTED" else 0.0,
        1.0 if classification == "ERROR" else 0.0,
        1.0 if row["canonical_entry_id"] else 0.0,
        float(row["requires_manual_approval"] or 0),
        float(row["confidence"] or 0.0),
        math.log10((row["size_bytes"] or 0) + 1),
        1.0 if suffix in _IMAGE else 0.0,
        1.0 if suffix in _DOCUMENT else 0.0,
        1.0 if suffix in _ARCHIVE else 0.0,
    ]
