"""Archive-versus-directory equivalence.

Compares an archive's streamed member content hashes against live directory subtrees to detect
``ARCHIVE_OF_DIRECTORY`` / ``ARCHIVE_PARTIAL_SNAPSHOT_OF``. Archives are never extracted; the
member content is streamed. Unique members are always enumerable before an archive is reviewed.
"""

from __future__ import annotations

import hashlib
import tarfile
import zipfile
from collections import defaultdict
from pathlib import Path

from ..relationships import upsert_content_relationship
from .archives import detect_archive_kind

ALGORITHM = "archive_directory"
ALGORITHM_VERSION = "1"
_MAX_BYTES = 256 * 1024 * 1024


def _member_hashes(path: Path, max_members: int) -> set[str] | None:
    kind = detect_archive_kind(path)
    hashes: set[str] = set()
    try:
        if kind == "zip":
            with zipfile.ZipFile(path) as archive:
                infos = archive.infolist()
                if len(infos) > max_members or sum(max(0, i.file_size) for i in infos) > _MAX_BYTES:
                    return None
                for info in infos:
                    if info.is_dir():
                        continue
                    digest = hashlib.sha256()
                    with archive.open(info) as stream:
                        for chunk in iter(lambda: stream.read(1 << 20), b""):
                            digest.update(chunk)
                    hashes.add(digest.hexdigest())
        elif kind == "tar":
            with tarfile.open(path) as archive:
                members = archive.getmembers()
                if len(members) > max_members or sum(max(0, m.size) for m in members) > _MAX_BYTES:
                    return None
                for member in members:
                    if not member.isfile():
                        continue
                    handle = archive.extractfile(member)
                    if handle is None:
                        continue
                    digest = hashlib.sha256()
                    for chunk in iter(lambda: handle.read(1 << 20), b""):
                        digest.update(chunk)
                    hashes.add(digest.hexdigest())
        else:
            return None
    except (OSError, zipfile.BadZipFile, tarfile.TarError):
        return None
    return hashes


def _top_level_directory_hashes(database):
    """(source_root, top_level) -> (dir_entry_id, set(full_hash))."""
    hashes: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in database.iter_rows(
        """SELECT e.source_root AS root,
              CASE WHEN instr(e.relative_path,'/')=0 THEN e.relative_path ELSE substr(e.relative_path,1,instr(e.relative_path,'/')-1) END AS top_level,
              s.full_hash AS h
           FROM filesystem_entries e JOIN file_signatures s ON s.entry_id=e.id
           WHERE e.entry_type='file' AND s.full_hash IS NOT NULL"""
    ):
        hashes[(str(row["root"]), str(row["top_level"]))].add(str(row["h"]))
    entry_ids: dict[tuple[str, str], int] = {}
    for row in database.iter_rows(
        "SELECT id,source_root,relative_path FROM filesystem_entries WHERE entry_type='directory' AND instr(relative_path,'/')=0"
    ):
        entry_ids[(str(row["source_root"]), str(row["relative_path"]))] = int(row["id"])
    return hashes, entry_ids


def run_archive_directory_analysis(database, config, scope=None, job_id=None) -> dict[str, int]:
    from ..collections.marginal_value import _ensure_all_hashed
    from ..jobs import checkpoint

    _ensure_all_hashed(database, config)
    max_members = config.section("archives")["max_members"]
    dir_hashes, dir_entry_ids = _top_level_directory_hashes(database)
    counts = {"relationships": 0}
    for index, archive in enumerate(
        database.fetch_all(
            "SELECT id,absolute_path FROM filesystem_entries WHERE entry_type='file' AND lower(suffix) IN ('.zip','.tar','.tgz','.gz')"
        ),
        1,
    ):
        checkpoint(database, job_id, processed_count=index, state={"last_archive_id": int(archive["id"])})
        members = _member_hashes(Path(archive["absolute_path"]), max_members)
        if not members:
            continue
        for key, hashes in dir_hashes.items():
            shared = len(members & hashes)
            containment = shared / len(members)
            if containment < 0.9 or key not in dir_entry_ids:
                continue
            relationship = "ARCHIVE_OF_DIRECTORY" if containment == 1.0 else "ARCHIVE_PARTIAL_SNAPSHOT_OF"
            tier = "TIER_2_NORMALIZED_EXACT" if containment == 1.0 else "TIER_4_PARTIAL_OVERLAP"
            upsert_content_relationship(
                database,
                "ARCHIVE",
                int(archive["id"]),
                "DIRECTORY",
                dir_entry_ids[key],
                relationship,
                tier,
                containment,
                ALGORITHM,
                ALGORITHM_VERSION,
                "1",
                {
                    "member_count": len(members),
                    "members_in_directory": shared,
                    "members_unique_to_archive": len(members) - shared,
                },
                f"{shared}/{len(members)} archive members present in the directory; "
                f"{len(members) - shared} unique to the archive (enumerate before review).",
            )
            counts["relationships"] += 1
    return counts
