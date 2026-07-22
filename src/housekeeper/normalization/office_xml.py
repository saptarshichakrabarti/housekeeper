"""Office Open XML package normalization (DOCX / XLSX / PPTX).

The normalized hash is the multiset of ``(member path, member content SHA-256)`` for every
package member *except* volatile property parts (docProps/*). This makes two packages that
differ only by ZIP member ordering, compression, or timestamps compare equal, while any change
to real content (text, formulas, tracked changes, embedded media, macros) still differs.
"""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

from .model import NormalizationProfile, NormalizedArtifact

# Property parts that carry timestamps / application version and must not affect equivalence.
VOLATILE_MEMBERS = frozenset(
    {"docProps/core.xml", "docProps/app.xml", "docProps/custom.xml"}
)

OFFICE_PACKAGE_PROFILE = NormalizationProfile(
    name="OFFICE_PACKAGE_EQUIVALENCE",
    content_kind="office",
    algorithm="ooxml_package_member_content_multiset",
    algorithm_version="1",
    configuration={"exclude": sorted(VOLATILE_MEMBERS)},
    loss_characteristics=(
        "zip_member_ordering",
        "zip_timestamps",
        "compression_method",
        "document_properties",
    ),
)


def normalize_office(path: Path, config) -> NormalizedArtifact:
    try:
        with zipfile.ZipFile(path) as package:
            entries = []
            for info in package.infolist():
                if info.is_dir() or info.filename in VOLATILE_MEMBERS:
                    continue
                digest = hashlib.sha256()
                with package.open(info) as member:
                    for chunk in iter(lambda: member.read(1 << 20), b""):
                        digest.update(chunk)
                entries.append((info.filename, digest.hexdigest()))
            entries.sort()
            manifest = "\n".join(f"{name}:{digest}" for name, digest in entries)
            member_paths = "\n".join(sorted(name for name, _ in entries))
            return NormalizedArtifact(
                status="OK",
                normalized_hash=hashlib.sha256(manifest.encode()).hexdigest(),
                normalized_size_bytes=len(manifest),
                structural_fingerprint=hashlib.sha256(member_paths.encode()).hexdigest(),
                artifact={"member_count": len(entries), "volatile_excluded": sorted(VOLATILE_MEMBERS)},
            )
    except (OSError, zipfile.BadZipFile) as exc:
        return NormalizedArtifact("ERROR", error_code=type(exc).__name__, error_message=str(exc))
