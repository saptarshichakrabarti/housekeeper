"""Normalization profile registry: maps file types to profiles and persists them."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

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

_ENABLE_SETTING = {
    OFFICE_PACKAGE_PROFILE.name: "office",
    ARCHIVE_CONTENT_PROFILE.name: "archives",
    PDF_TEXT_PROFILE.name: "pdf",
}


def enabled_profiles(config) -> tuple[NormalizationProfile, ...]:
    """Profiles enabled by operator-facing normalization switches.

    Profiles without a top-level enable switch remain active. Keeping this decision in the
    registry ensures hashing, normalization, and relationship emission use the same profile set.
    """
    section = config.section("normalization")
    return tuple(
        profile
        for profile in ALL_PROFILES
        if (setting := _ENABLE_SETTING.get(profile.name)) is None
        or bool(section[setting].get("enabled", True))
    )


#: Which suffixes each profile claims. Replaces the old suffix -> profiles lookup, which forced the
#: caller to iterate objects; profile -> suffixes lets it ask "which objects does this profile still
#: owe an artifact for" as one SQL anti-join per profile instead.
PROFILE_SUFFIXES: dict[str, frozenset[str]] = {
    IMAGE_PIXEL_PROFILE.name: frozenset(_IMAGE_SUFFIXES),
    OFFICE_PACKAGE_PROFILE.name: frozenset(_OFFICE_SUFFIXES),
    ARCHIVE_CONTENT_PROFILE.name: frozenset(_ARCHIVE_SUFFIXES),
    PDF_TEXT_PROFILE.name: frozenset(_PDF_SUFFIXES),
    AUDIO_PAYLOAD_PROFILE.name: frozenset(_AUDIO_SUFFIXES),
    TABULAR_PROFILE.name: frozenset(_TABULAR_SUFFIXES),
}

_NORMALIZER_OF = {
    IMAGE_PIXEL_PROFILE.name: normalize_image,
    OFFICE_PACKAGE_PROFILE.name: normalize_office,
    ARCHIVE_CONTENT_PROFILE.name: normalize_archive_content,
    PDF_TEXT_PROFILE.name: normalize_pdf,
    AUDIO_PAYLOAD_PROFILE.name: normalize_audio,
    TABULAR_PROFILE.name: normalize_tabular,
}


def normalizer_for(profile: NormalizationProfile) -> Normalizer:
    return _NORMALIZER_OF[profile.name]


def supported_suffixes(config=None) -> set[str]:
    profiles = ALL_PROFILES if config is None else enabled_profiles(config)
    return set().union(*(PROFILE_SUFFIXES[profile.name] for profile in profiles))


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
