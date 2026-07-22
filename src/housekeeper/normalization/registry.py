"""Normalization profile registry: maps file types to profiles and persists them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable

from .archives import ARCHIVE_CONTENT_PROFILE, normalize_archive_content
from .audio import AUDIO_PAYLOAD_PROFILE, normalize_audio
from .images import IMAGE_PIXEL_PROFILE, normalize_image
from .model import NormalizationProfile, NormalizedArtifact
from .office_xml import OFFICE_PACKAGE_PROFILE, normalize_office
from .pdf import PDF_TEXT_PROFILE, normalize_pdf
from .tabular import TABULAR_PROFILE, normalize_tabular

Normalizer = Callable[[Path, object], NormalizedArtifact]

_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".tiff", ".bmp"}
_OFFICE_SUFFIXES = {".docx", ".xlsx", ".xlsm", ".pptx"}
_ARCHIVE_SUFFIXES = {".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz"}
_PDF_SUFFIXES = {".pdf"}
_AUDIO_SUFFIXES = {".mp3"}
_TABULAR_SUFFIXES = {".csv", ".tsv"}

# The relationship type each profile's normalized-hash match implies, with its evidence tier.
PROFILE_RELATIONSHIP = {
    IMAGE_PIXEL_PROFILE.name: ("PIXEL_IDENTICAL", "TIER_2_NORMALIZED_EXACT"),
    OFFICE_PACKAGE_PROFILE.name: ("OFFICE_PACKAGE_EQUIVALENT", "TIER_2_NORMALIZED_EXACT"),
    ARCHIVE_CONTENT_PROFILE.name: ("ARCHIVE_REPACKAGING_VARIANT", "TIER_2_NORMALIZED_EXACT"),
    PDF_TEXT_PROFILE.name: ("PDF_TEXT_EQUIVALENT", "TIER_3_STRONG_EQUIVALENCE"),
    AUDIO_PAYLOAD_PROFILE.name: ("AUDIO_TAG_VARIANT", "TIER_2_NORMALIZED_EXACT"),
    TABULAR_PROFILE.name: ("TABULAR_CONTENT_EQUIVALENT", "TIER_3_STRONG_EQUIVALENCE"),
}

ALL_PROFILES = (
    IMAGE_PIXEL_PROFILE,
    OFFICE_PACKAGE_PROFILE,
    ARCHIVE_CONTENT_PROFILE,
    PDF_TEXT_PROFILE,
    AUDIO_PAYLOAD_PROFILE,
    TABULAR_PROFILE,
)


def normalizers_for(suffix: str) -> list[tuple[NormalizationProfile, Normalizer]]:
    s = suffix.lower()
    result: list[tuple[NormalizationProfile, Normalizer]] = []
    if s in _IMAGE_SUFFIXES:
        result.append((IMAGE_PIXEL_PROFILE, normalize_image))
    if s in _OFFICE_SUFFIXES:
        result.append((OFFICE_PACKAGE_PROFILE, normalize_office))
    if s in _ARCHIVE_SUFFIXES:
        result.append((ARCHIVE_CONTENT_PROFILE, normalize_archive_content))
    if s in _PDF_SUFFIXES:
        result.append((PDF_TEXT_PROFILE, normalize_pdf))
    if s in _AUDIO_SUFFIXES:
        result.append((AUDIO_PAYLOAD_PROFILE, normalize_audio))
    if s in _TABULAR_SUFFIXES:
        result.append((TABULAR_PROFILE, normalize_tabular))
    return result


def supported_suffixes() -> set[str]:
    return (
        _IMAGE_SUFFIXES
        | _OFFICE_SUFFIXES
        | _ARCHIVE_SUFFIXES
        | _PDF_SUFFIXES
        | _AUDIO_SUFFIXES
        | _TABULAR_SUFFIXES
    )


def get_or_create_profile_id(database, profile: NormalizationProfile) -> int:
    fingerprint = profile.fingerprint()
    database.connect().execute(
        """INSERT OR IGNORE INTO normalization_profiles(name,content_kind,algorithm,algorithm_version,configuration_json,configuration_fingerprint,loss_characteristics_json)
           VALUES(?,?,?,?,?,?,?)""",
        (
            profile.name,
            profile.content_kind,
            profile.algorithm,
            profile.algorithm_version,
            json.dumps(profile.configuration, sort_keys=True),
            fingerprint,
            json.dumps(list(profile.loss_characteristics)),
        ),
    )
    database.connect().commit()
    row = database.fetch_one(
        "SELECT id FROM normalization_profiles WHERE name=? AND algorithm_version=? AND configuration_fingerprint=?",
        (profile.name, profile.algorithm_version, fingerprint),
    )
    assert row is not None
    return int(row["id"])
