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

from ..hashing import compute_full_hash
from ..normalization.registry import (
    ALL_PROFILES,
    PROFILE_RELATIONSHIP,
    get_or_create_profile_id,
    normalizers_for,
    supported_suffixes,
)
from ..relationships import invalidate_content_relationships, upsert_content_relationship

analyseR_NAME = "normalized_content"
analyseR_VERSION = "1"

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


def _allowed_content_objects(database, scope) -> set[int] | None:
    if scope is None:
        return None
    from .scope import scoped_entry_ids

    entry_ids = scoped_entry_ids(database, scope)
    if not entry_ids:
        return set()
    marks = ",".join("?" for _ in entry_ids)
    return {
        int(r["content_object_id"])
        for r in database.fetch_all(
            f"SELECT DISTINCT content_object_id FROM entry_content_links WHERE entry_id IN ({marks})",
            tuple(entry_ids),
        )
    }


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
    database.connect().commit()


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


def _normalize_objects(database, config, allowed, job_id, counts) -> None:
    from ..jobs import check_cancelled, update_job

    profile_ids = {profile.name: get_or_create_profile_id(database, profile) for profile in ALL_PROFILES}
    objects = database.fetch_all("SELECT id FROM content_objects ORDER BY id")
    for index, obj in enumerate(objects, start=1):
        content_object_id = int(obj["id"])
        if allowed is not None and content_object_id not in allowed:
            continue
        if job_id:
            check_cancelled(database, job_id)
        reps = _representative_entries(database, content_object_id)
        if not reps:
            continue
        suffix = (reps[0]["suffix"] or "").lower()
        for profile, normalizer in normalizers_for(suffix):
            artifact = None
            for rep in reps:  # representative-path fallback
                path = Path(rep["absolute_path"])
                if not path.is_file() or path.is_symlink():
                    continue
                artifact = normalizer(path, config)
                if artifact.status == "OK":
                    artifact.artifact["representative_entry_id"] = int(rep["id"])
                    break
            if artifact is None:
                continue
            _store_artifact(database, content_object_id, profile_ids[profile.name], profile, artifact)
            key = "normalized" if artifact.status == "OK" else ("errors" if artifact.status == "ERROR" else "unsupported")
            counts[key] += 1
        if job_id:
            update_job(database, job_id, "RUNNING", processed_count=index)


def _ensure_content_objects(database, config, allowed_entries: set[int] | None) -> None:
    """Hash supported-suffix files that are not yet linked to a content object.

    Makes ``analyse normalized-content`` self-sufficient after a bare scan: identity analysis
    otherwise only hashes exact-duplicate candidates, so format-equivalent (byte-different)
    files would have no content object to normalize.
    """
    suffixes = supported_suffixes()
    algorithm = config.section("hashing")["algorithm"]
    block = config.section("hashing")["full_hash_block_bytes"]
    for row in database.iter_rows(
        """SELECT e.id,e.absolute_path,e.suffix FROM filesystem_entries e
           LEFT JOIN entry_content_links l ON l.entry_id=e.id
           WHERE e.entry_type='file' AND l.entry_id IS NULL"""
    ):
        if allowed_entries is not None and int(row["id"]) not in allowed_entries:
            continue
        if (row["suffix"] or "").lower() not in suffixes:
            continue
        path = Path(row["absolute_path"])
        if not path.is_file() or path.is_symlink():
            continue
        result = compute_full_hash(path, algorithm, block)
        if not result.stable or not result.digest:
            continue
        cid = database.get_or_create_content_object(algorithm, result.digest, result.size)
        database.connect().execute(
            "INSERT OR REPLACE INTO file_signatures(entry_id,full_hash,hash_algorithm,hash_status,full_hash_computed_at) VALUES(?,?,?, 'VERIFIED', CURRENT_TIMESTAMP)",
            (int(row["id"]), result.digest, algorithm),
        )
        database.link_entry_content(int(row["id"]), cid, "", "VERIFIED")
    database.connect().commit()


def run_normalized_content_analysis(database, config, scope=None, job_id=None) -> dict[str, int]:
    allowed_entries = None
    if scope is not None:
        from .scope import scoped_entry_ids

        allowed_entries = scoped_entry_ids(database, scope)
    _ensure_content_objects(database, config, allowed_entries)
    allowed = _allowed_content_objects(database, scope)
    for profile in ALL_PROFILES:  # supersede any relationships from an older version/config
        invalidate_content_relationships(
            database, profile.algorithm, profile.algorithm_version, profile.fingerprint()
        )
    counts = {"normalized": 0, "errors": 0, "unsupported": 0, "relationships": 0}
    _normalize_objects(database, config, allowed, job_id, counts)

    for profile in ALL_PROFILES:
        relationship_type, tier = PROFILE_RELATIONSHIP[profile.name]
        profile_id = get_or_create_profile_id(database, profile)
        groups: dict[str, list[int]] = {}
        orientation_groups: dict[str, list[int]] = {}
        pixel_hash_of: dict[int, str] = {}
        for row in database.iter_rows(
            "SELECT content_object_id,normalized_hash,artifact_json FROM normalized_content_artifacts WHERE normalization_profile_id=? AND status='OK' AND normalized_hash IS NOT NULL",
            (profile_id,),
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
