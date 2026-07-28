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

#: The cached member-digest summary, keyed by content identity in ``similarity_signatures``.
#: Digests only — never extracted member bytes — so the streamed-summary safety guarantee is
#: unchanged. Keyed by content object, so N copies of one archive stream once, and a rescan of an
#: unchanged archive streams nothing at all.
_SIGNATURE_TYPE = "ARCHIVE_MEMBER_DIGESTS"
_SIGNATURE_VERSION = "1"


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
                    # Walrus rather than iter(lambda: handle.read(...)): the lambda closed over the
                    # loop variable, so it read whichever member the loop had reached rather than
                    # the one being digested. Correct only because it was consumed immediately.
                    while chunk := handle.read(1 << 20):
                        digest.update(chunk)
                    hashes.add(digest.hexdigest())
        else:
            return None
    except (OSError, zipfile.BadZipFile, tarfile.TarError):
        return None
    return hashes


def _cached_member_hashes(database, content_object_id, path: Path, max_members: int):
    """``_member_hashes`` memoised on content identity. ``None`` means "out of bounds, skipped"."""
    if content_object_id is None:
        return _member_hashes(path, max_members)
    key = (int(content_object_id), _SIGNATURE_TYPE, _SIGNATURE_VERSION, str(max_members))
    row = database.fetch_one(
        """SELECT signature_blob,status FROM similarity_signatures
           WHERE content_object_id=? AND signature_type=? AND signature_version=?
             AND configuration_fingerprint=?""",
        key,
    )
    if row is not None:
        if row["status"] != "OK":
            return None
        blob = row["signature_blob"] or ""
        return set(blob.split("\n")) if blob else set()
    hashes = _member_hashes(path, max_members)
    # The skip is cached too: deciding an archive is out of bounds costs a full member scan on tar.
    database.connect().execute(
        """INSERT OR IGNORE INTO similarity_signatures(content_object_id,signature_type,
             signature_version,configuration_fingerprint,signature_blob,feature_count,status)
           VALUES(?,?,?,?,?,?,?)""",
        (
            *key,
            "\n".join(sorted(hashes)) if hashes is not None else None,
            len(hashes) if hashes is not None else None,
            "OK" if hashes is not None else "SKIPPED",
        ),
    )
    return hashes


def _top_level_directory_hashes(database, scope):
    """(source_root, top_level) -> (dir_entry_id, set(full_hash))."""
    hashes: dict[tuple[str, str], set[str]] = defaultdict(set)
    entry_sql, params = scope.entry_id_sql()
    for row in database.iter_rows(
        f"""SELECT e.source_root AS root,
              CASE WHEN instr(e.relative_path,'/')=0 THEN e.relative_path ELSE substr(e.relative_path,1,instr(e.relative_path,'/')-1) END AS top_level,
              s.full_hash AS h
           FROM filesystem_entries e JOIN file_signatures s ON s.entry_id=e.id
           WHERE e.entry_type='file' AND s.full_hash IS NOT NULL AND e.id IN ({entry_sql})""",
        params,
    ):
        hashes[(str(row["root"]), str(row["top_level"]))].add(str(row["h"]))
    entry_ids: dict[tuple[str, str], int] = {}
    directory_sql, directory_params = scope.entry_id_sql("directory")
    for row in database.iter_rows(
        "SELECT id,source_root,relative_path FROM filesystem_entries "
        f"WHERE entry_type='directory' AND instr(relative_path,'/')=0 AND id IN ({directory_sql})",
        directory_params,
    ):
        entry_ids[(str(row["source_root"]), str(row["relative_path"]))] = int(row["id"])
    return hashes, entry_ids


def run_archive_directory_analysis(database, config, scope=None, job_id=None) -> dict[str, int]:
    from ..collections.marginal_value import _ensure_all_hashed
    from ..jobs import checkpoint
    from .scope import resolve_scope

    scope = resolve_scope(database, scope)
    entry_sql, scope_params = scope.entry_id_sql()
    _ensure_all_hashed(database, config, scope)
    max_members = config.section("archives")["max_members"]
    dir_hashes, dir_entry_ids = _top_level_directory_hashes(database, scope)
    counts = {"relationships": 0}
    for index, archive in enumerate(
        database.fetch_all(
            "SELECT e.id,e.absolute_path,l.content_object_id FROM filesystem_entries e "
            "LEFT JOIN entry_content_links l ON l.entry_id=e.id AND l.link_status='VERIFIED' "
            "WHERE e.entry_type='file' "
            f"AND lower(e.suffix) IN ('.zip','.tar','.tgz','.gz') AND e.id IN ({entry_sql})",
            scope_params,
        ),
        1,
    ):
        checkpoint(database, job_id, processed_count=index, state={"last_archive_id": int(archive["id"])})
        members = _cached_member_hashes(
            database,
            archive["content_object_id"],
            Path(archive["absolute_path"]),
            max_members,
        )
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
    # This analyser is a stage: the write primitives no longer commit per row, so the one
    # commit that makes its work durable belongs here.
    database.connect().commit()
    return counts
