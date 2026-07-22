"""Archive content normalization (ZIP / TAR family).

The normalized hash is the multiset of ``(normalized member path, member content SHA-256)``
computed by *streaming* member content — archives are never extracted to disk. This makes two
archives that differ only in compression, member ordering, or timestamps compare equal, while
any change to member content still differs. Oversized archives are reported UNSUPPORTED rather
than doing unbounded work.
"""

from __future__ import annotations

import hashlib
import tarfile
import zipfile
from pathlib import Path

from ..analyzers.archives import detect_archive_kind, normalize_archive_member_path
from .model import NormalizationProfile, NormalizedArtifact

# Beyond this declared uncompressed size the content multiset is not computed (kept bounded).
MAX_CONTENT_BYTES = 256 * 1024 * 1024

ARCHIVE_CONTENT_PROFILE = NormalizationProfile(
    name="ARCHIVE_CONTENT_EQUIVALENCE",
    content_kind="archive",
    algorithm="member_content_multiset",
    algorithm_version="1",
    configuration={"max_content_bytes": MAX_CONTENT_BYTES},
    loss_characteristics=("member_ordering", "member_timestamps", "compression_method"),
)


def _finalize(entries: list[tuple[str, str]], paths: list[tuple[str, int]]) -> NormalizedArtifact:
    entries.sort()
    manifest = "\n".join(f"{name}:{digest}" for name, digest in entries)
    path_manifest = "\n".join(f"{name}:{size}" for name, size in sorted(paths))
    return NormalizedArtifact(
        status="OK",
        normalized_hash=hashlib.sha256(manifest.encode()).hexdigest(),
        normalized_size_bytes=len(manifest),
        structural_fingerprint=hashlib.sha256(path_manifest.encode()).hexdigest(),
        artifact={"member_count": len(entries)},
    )


def normalize_archive_content(path: Path, config) -> NormalizedArtifact:
    kind = detect_archive_kind(path)
    max_members = config.section("archives")["max_members"]
    max_content = int(
        config.data.get("normalization", {}).get("archives", {}).get("max_content_bytes", MAX_CONTENT_BYTES)
    )
    try:
        if kind == "zip":
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                if len(infos) > max_members:
                    return NormalizedArtifact("UNSUPPORTED", error_code="too_many_members")
                if sum(max(0, i.file_size) for i in infos) > max_content:
                    return NormalizedArtifact("UNSUPPORTED", error_code="content_too_large")
                entries, paths = [], []
                for info in infos:
                    if info.is_dir():
                        continue
                    name = normalize_archive_member_path(info.filename)
                    digest = hashlib.sha256()
                    with archive.open(info) as stream:
                        for chunk in iter(lambda: stream.read(1 << 20), b""):
                            digest.update(chunk)
                    entries.append((name, digest.hexdigest()))
                    paths.append((name, info.file_size))
                return _finalize(entries, paths)
        elif kind == "tar":
            with tarfile.open(path) as archive:
                members = archive.getmembers()
                if len(members) > max_members:
                    return NormalizedArtifact("UNSUPPORTED", error_code="too_many_members")
                if sum(max(0, m.size) for m in members) > max_content:
                    return NormalizedArtifact("UNSUPPORTED", error_code="content_too_large")
                entries, paths = [], []
                for member in members:
                    if not member.isfile():
                        continue
                    name = normalize_archive_member_path(member.name)
                    handle = archive.extractfile(member)
                    if handle is None:
                        continue
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: handle.read(1 << 20), b""):
                        digest.update(chunk)
                    entries.append((name, digest.hexdigest()))
                    paths.append((name, member.size))
                return _finalize(entries, paths)
        return NormalizedArtifact("UNSUPPORTED", error_code="not_an_archive")
    except (OSError, zipfile.BadZipFile, tarfile.TarError) as exc:
        return NormalizedArtifact("ERROR", error_code=type(exc).__name__, error_message=str(exc))
