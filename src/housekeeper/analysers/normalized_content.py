"""Format-aware normalized-equivalence analyser.

For each unique content object it computes deterministic, versioned normalized fingerprints
(one parse per content object, with representative-path fallback), stores them, and emits
*tiered* ``content_relationships``:

* two content objects with the same decoded pixels but different bytes -> ``PIXEL_IDENTICAL``
  (Tier 2), and ``ORIENTATION_VARIANT`` (Tier 3) when only the EXIF-oriented pixels match;
* two Office packages with the same member content multiset -> ``OFFICE_PACKAGE_EQUIVALENT``;
* two archives with the same member-content multiset -> ``ARCHIVE_REPACKAGING_VARIANT``.

None of these is ever a byte-identical exact duplicate: distinct content objects have distinct
raw SHA-256 by construction, so a normalized match is always strictly weaker than Tier 1.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..normalization.registry import (
    ALL_PROFILES,
    PROFILE_RELATIONSHIP,
    PROFILE_SUFFIXES,
    get_or_create_profile_id,
    normalizer_for,
    supported_suffixes,
)
from ..relationships import invalidate_content_relationships, upsert_content_relationship

ANALYSER_NAME = "normalized_content"
ANALYSER_VERSION = "1"

_SIGNATURE_TYPE = {
    "IMAGE_PIXEL_EQUIVALENCE": "PIXEL_HASH",
    "OFFICE_PACKAGE_EQUIVALENCE": "OFFICE_STRUCTURAL_HASH",
    "ARCHIVE_CONTENT_EQUIVALENCE": "ARCHIVE_MANIFEST_HASH",
}
_MAX_PAIRWISE_GROUP = 16


def _representative_entries(database, content_object_id: int):
    return database.fetch_all(
        """SELECT e.id,e.absolute_path,e.suffix FROM entry_content_links l
           JOIN filesystem_entries e ON e.id=l.entry_id
           WHERE l.content_object_id=? AND e.entry_type='file' ORDER BY e.id""",
        (content_object_id,),
    )




def _store_artifact(database, content_object_id, profile_id, profile, artifact) -> None:
    database.connect().execute(
        """INSERT INTO normalized_content_artifacts(content_object_id,normalization_profile_id,status,normalized_hash,normalized_size_bytes,structural_fingerprint,artifact_json,error_code,error_message)
           VALUES(?,?,?,?,?,?,?,?,?)
           ON CONFLICT(content_object_id,normalization_profile_id) DO UPDATE SET status=excluded.status,
           normalized_hash=excluded.normalized_hash,normalized_size_bytes=excluded.normalized_size_bytes,
           structural_fingerprint=excluded.structural_fingerprint,artifact_json=excluded.artifact_json,
           error_code=excluded.error_code,error_message=excluded.error_message,created_at=CURRENT_TIMESTAMP""",
        (
            content_object_id,
            profile_id,
            artifact.status,
            artifact.normalized_hash,
            artifact.normalized_size_bytes,
            artifact.structural_fingerprint,
            json.dumps(artifact.artifact, sort_keys=True),
            artifact.error_code,
            artifact.error_message,
        ),
    )
    if artifact.status == "OK" and artifact.normalized_hash:
        database.connect().execute(
            """INSERT OR IGNORE INTO similarity_signatures(content_object_id,signature_type,signature_version,configuration_fingerprint,signature_blob,feature_count,status)
               VALUES(?,?,?,?,?,?, 'OK')""",
            (
                content_object_id,
                _SIGNATURE_TYPE.get(profile.name, profile.name),
                profile.algorithm_version,
                profile.fingerprint(),
                artifact.normalized_hash,
                artifact.artifact.get("member_count"),
            ),
        )
    # No commit: tracked_job owns the transaction for the whole stage, and committing per
    # normalized object turned one transaction into one per file in the corpus.


def _emit_group(database, profile, content_ids, relationship_type, tier, evidence_key) -> None:
    """Emit pairwise (bounded) or star relationships for a normalized-equivalence group."""
    ordered = sorted(content_ids)
    pairs = (
        [(ordered[i], ordered[j]) for i in range(len(ordered)) for j in range(i + 1, len(ordered))]
        if len(ordered) <= _MAX_PAIRWISE_GROUP
        else [(ordered[0], other) for other in ordered[1:]]
    )
    for a, b in pairs:
        upsert_content_relationship(
            database,
            "CONTENT_OBJECT",
            a,
            "CONTENT_OBJECT",
            b,
            relationship_type,
            tier,
            1.0,
            profile.algorithm,
            profile.algorithm_version,
            profile.fingerprint(),
            {"normalized_hash": evidence_key, "profile": profile.name, "group_size": len(ordered)},
            f"Normalized-equal under {profile.name} (ignores {', '.join(profile.loss_characteristics)}).",
        )


def _pending_objects(database, scope, profile, profile_id):
    """Content objects this profile still owes an artifact for — one query, not one per object.

    The schema has always carried ``UNIQUE(content_object_id, normalization_profile_id)``; the
    stage simply never asked it anything, and re-normalised the whole corpus every run. A profile
    id changes with its version and configuration fingerprint, so a genuine profile change still
    re-does the work.
    """
    suffixes = sorted(PROFILE_SUFFIXES[profile.name])
    content_sql, params = scope.content_object_id_sql()
    return database.reader().iter_rows(
        f"""SELECT DISTINCT co.id FROM content_objects co
            JOIN entry_content_links l ON l.content_object_id=co.id
            JOIN filesystem_entries e ON e.id=l.entry_id AND e.entry_type='file'
            WHERE co.id IN ({content_sql})
              AND lower(e.suffix) IN ({",".join("?" for _ in suffixes)})
              AND NOT EXISTS(SELECT 1 FROM normalized_content_artifacts n
                             WHERE n.content_object_id=co.id AND n.normalization_profile_id=?)
            ORDER BY co.id""",
        (*params, *suffixes, profile_id),
    )


def _normalize_objects(database, config, scope, job_id, counts) -> None:
    from ..jobs import checkpoint

    processed = 0
    for profile in ALL_PROFILES:
        profile_id = get_or_create_profile_id(database, profile)
        normalizer = normalizer_for(profile)
        for row in _pending_objects(database, scope, profile, profile_id):
            content_object_id = int(row["id"])
            processed += 1
            artifact = None
            for rep in _representative_entries(database, content_object_id):  # fallback path
                path = Path(rep["absolute_path"])
                if not path.is_file() or path.is_symlink():
                    continue
                artifact = normalizer(path, config)
                if artifact.status == "OK":
                    artifact.artifact["representative_entry_id"] = int(rep["id"])
                    break
            if artifact is None:
                continue
            _store_artifact(database, content_object_id, profile_id, profile, artifact)
            key = "normalized" if artifact.status == "OK" else ("errors" if artifact.status == "ERROR" else "unsupported")
            counts[key] += 1
            # checkpoint(), not update_job(..., "RUNNING", ...): any non-null status commits, so
            # re-asserting a status the job already has published one transaction per object purely
            # to move a progress bar. checkpoint() polls cancellation and rate-limits the commit.
            checkpoint(database, job_id, processed_count=processed)


def _ensure_content_objects(database, config, scope) -> None:
    """Hash supported-suffix files that are not yet linked to a content object.

    Makes ``analyse normalized-content`` self-sufficient after a bare scan: identity analysis
    otherwise only hashes exact-duplicate candidates, so format-equivalent (byte-different)
    files would have no content object to normalize.
    """
    from ..core.identity import ensure_content_identity, stream_identity_candidates

    suffixes = [s.lower() for s in supported_suffixes()]
    entry_sql, params = scope.entry_id_sql()
    marks = ",".join("?" for _ in suffixes)
    # The suffix filter is in SQL now, not a Python skip after fetching: the shared identity service
    # consumes the stream directly, and there is no per-row work left to do here.
    stream = stream_identity_candidates(
        database.reader(),
        f"""SELECT e.id,e.scan_run_id,e.absolute_path,e.device_id,e.inode_or_file_id,e.nlink
           FROM filesystem_entries e LEFT JOIN entry_content_links l ON l.entry_id=e.id
           WHERE e.entry_type='file' AND l.entry_id IS NULL AND e.id IN ({entry_sql})
           AND lower(e.suffix) IN ({marks}){{keyset}}""",
        (*params, *suffixes),
    )
    ensure_content_identity(database, config, stream, progress_phase="hashing for normalization")


def run_normalized_content_analysis(database, config, scope=None, job_id=None) -> dict[str, int]:
    from .scope import resolve_scope

    scope = resolve_scope(database, scope)
    _ensure_content_objects(database, config, scope)
    for profile in ALL_PROFILES:  # supersede any relationships from an older version/config
        invalidate_content_relationships(
            database, profile.algorithm, profile.algorithm_version, profile.fingerprint()
        )
    counts = {"normalized": 0, "errors": 0, "unsupported": 0, "relationships": 0}
    _normalize_objects(database, config, scope, job_id, counts)

    # Group emission is scoped too. It used to read *every* normalized artifact per profile to
    # re-derive equivalence groups — no file I/O, but O(corpus) per run regardless of how little
    # changed, and it related content objects reachable only from snapshots nobody asked about. One
    # `content_object_id IN (scope)` makes the stage proportional to the drive.
    content_sql, content_params = scope.content_object_id_sql()
    for profile in ALL_PROFILES:
        relationship_type, tier = PROFILE_RELATIONSHIP[profile.name]
        profile_id = get_or_create_profile_id(database, profile)
        groups: dict[str, list[int]] = {}
        orientation_groups: dict[str, list[int]] = {}
        pixel_hash_of: dict[int, str] = {}
        for row in database.iter_rows(
            "SELECT content_object_id,normalized_hash,artifact_json FROM normalized_content_artifacts "
            f"WHERE normalization_profile_id=? AND status='OK' AND normalized_hash IS NOT NULL "
            f"AND content_object_id IN ({content_sql})",
            (profile_id, *content_params),
        ):
            cid = int(row["content_object_id"])
            groups.setdefault(row["normalized_hash"], []).append(cid)
            pixel_hash_of[cid] = row["normalized_hash"]
            orient = json.loads(row["artifact_json"] or "{}").get("orientation_normalized_hash")
            if orient:
                orientation_groups.setdefault(orient, []).append(cid)
        for normalized_hash, cids in groups.items():
            unique = sorted(set(cids))
            if len(unique) >= 2:
                _emit_group(database, profile, unique, relationship_type, tier, normalized_hash)
                counts["relationships"] += 1
        if profile.name == "IMAGE_PIXEL_EQUIVALENCE":
            for orient, cids in orientation_groups.items():
                unique = sorted(set(cids))
                if len(unique) >= 2 and len({pixel_hash_of[c] for c in unique}) >= 2:
                    _emit_group(
                        database, profile, unique, "ORIENTATION_VARIANT", "TIER_3_STRONG_EQUIVALENCE", orient
                    )
                    counts["relationships"] += 1
    return counts
