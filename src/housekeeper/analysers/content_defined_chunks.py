"""Content-defined chunking analyser: partial-content overlap (Tier-4).

Selective and opt-in: only content objects at/above ``chunking.minimum_file_size_bytes`` are
chunked. Candidate pairs come from an inverted chunk index (never all-pairs). Overlap produces
``PARTIAL_CONTENT_OVERLAP`` / ``NEAR_SUBSET_CONTENT`` relationships at Tier 4 — never exact.
"""

from __future__ import annotations

from pathlib import Path

from ..chunking.backend import chunk_file
from ..chunking.index import store_chunks
from ..chunking.overlap import compute_overlap, generate_overlap_candidates
from ..chunking.profiles import get_or_create_chunk_profile_id, profile_from_config
from ..relationships import upsert_content_relationship

ALGORITHM = "fastcdc_gear"
ALGORITHM_VERSION = "1"


def _representative_path(database, content_object_id: int) -> Path | None:
    row = database.fetch_one(
        """SELECT e.absolute_path FROM entry_content_links l JOIN filesystem_entries e ON e.id=l.entry_id
           WHERE l.content_object_id=? AND e.entry_type='file' ORDER BY e.id LIMIT 1""",
        (content_object_id,),
    )
    if not row:
        return None
    path = Path(row["absolute_path"])
    return path if path.is_file() and not path.is_symlink() else None


def _ensure_hashed_large_files(database, config, minimum_file: int, scope) -> None:
    from ..core.identity import ensure_content_identity, stream_identity_candidates

    entry_sql, params = scope.entry_id_sql()
    stream = stream_identity_candidates(
        database.reader(),
        f"""SELECT e.id,e.scan_run_id,e.absolute_path,e.size_bytes,e.device_id,e.inode_or_file_id,e.nlink
           FROM filesystem_entries e LEFT JOIN entry_content_links l ON l.entry_id=e.id
           WHERE e.entry_type='file' AND l.entry_id IS NULL AND e.size_bytes>=?
           AND e.id IN ({entry_sql}){{keyset}}""",
        (minimum_file, *params),
    )
    ensure_content_identity(database, config, stream, progress_phase="hashing large files")


def _index_bytes(database, profile_id: int) -> int:
    """Bytes of source content the chunk index currently covers for this profile."""
    row = database.fetch_one(
        "SELECT COALESCE(SUM(size_bytes*occurrence_count),0) AS n FROM content_chunks "
        "WHERE chunking_profile_id=?",
        (profile_id,),
    )
    return int(row["n"]) if row else 0


def run_chunk_analysis(database, config, scope=None, job_id=None) -> dict[str, int]:
    from ..jobs import check_cancelled, checkpoint

    minimum_file = int(config.section("chunking")["minimum_file_size_bytes"])
    profile = profile_from_config(config)
    profile_id = get_or_create_chunk_profile_id(database, profile)
    from .scope import resolve_scope

    scope = resolve_scope(database, scope)
    _ensure_hashed_large_files(database, config, minimum_file, scope)
    counts = {"chunked": 0, "chunks": 0, "skipped": 0, "index_full": 0}
    # chunking.maximum_total_index_bytes was a stated cap on how large the chunk index may grow,
    # enforced nowhere. Checked here, between objects, so a run stops at the bound instead of
    # discovering it as a full disk.
    maximum_index = int(config.section("chunking")["maximum_total_index_bytes"])
    content_sql, content_params = scope.content_object_id_sql()
    objects = database.fetch_all(
        f"SELECT id FROM content_objects WHERE size_bytes>=? AND id IN ({content_sql}) ORDER BY id",
        (minimum_file, *content_params),
    )
    # Seed the covered-byte total once and maintain it by the net delta each store returns, instead
    # of re-aggregating the whole chunk index between every object (an O(index) query per object).
    index_bytes = _index_bytes(database, profile_id)
    for index, obj in enumerate(objects, start=1):
        if job_id:
            check_cancelled(database, job_id)
        if index_bytes >= maximum_index:
            counts["index_full"] = 1
            counts["skipped"] += len(objects) - index + 1
            break
        path = _representative_path(database, int(obj["id"]))
        if path is None:
            counts["skipped"] += 1
            continue
        records = list(chunk_file(path, profile))
        index_bytes += store_chunks(database, int(obj["id"]), profile_id, profile, records)
        counts["chunked"] += 1
        counts["chunks"] += len(records)
        checkpoint(database, job_id, processed_count=index)
    return counts


def run_chunk_overlap_analysis(database, config, job_id=None) -> dict[str, int]:
    from ..jobs import checkpoint

    section = config.section("chunking")
    profile = profile_from_config(config)
    profile_id = get_or_create_chunk_profile_id(database, profile)
    cutoff = int(section["common_chunk_frequency_cutoff"])
    minimum_overlap = int(section["minimum_overlap_bytes"])
    counts = {"pairs": 0, "relationships": 0}
    for index, (a_id, b_id) in enumerate(
        sorted(generate_overlap_candidates(database, profile_id, cutoff)), 1
    ):
        checkpoint(database, job_id, processed_count=index, state={"last_pair": [a_id, b_id]})
        scores = compute_overlap(database, a_id, b_id)
        if scores["shared_chunk_bytes"] < minimum_overlap:
            continue
        confidence = float(max(scores["overlap_a_in_b"], scores["overlap_b_in_a"]))
        database.connect().execute(
            """INSERT OR REPLACE INTO content_overlap_results(content_object_a_id,content_object_b_id,chunking_profile_id,
               shared_chunk_count,shared_chunk_bytes,a_total_chunk_bytes,b_total_chunk_bytes,overlap_a_in_b,overlap_b_in_a,weighted_jaccard,confidence)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (
                a_id,
                b_id,
                profile_id,
                scores["shared_chunk_count"],
                scores["shared_chunk_bytes"],
                scores["a_total_chunk_bytes"],
                scores["b_total_chunk_bytes"],
                scores["overlap_a_in_b"],
                scores["overlap_b_in_a"],
                scores["weighted_jaccard"],
                confidence,
            ),
        )
        counts["pairs"] += 1
        relationship = "NEAR_SUBSET_CONTENT" if confidence >= 0.9 else "PARTIAL_CONTENT_OVERLAP"
        upsert_content_relationship(
            database,
            "CONTENT_OBJECT",
            a_id,
            "CONTENT_OBJECT",
            b_id,
            relationship,
            "TIER_4_PARTIAL_OVERLAP",
            confidence,
            ALGORITHM,
            ALGORITHM_VERSION,
            profile.fingerprint(),
            {k: v for k, v in scores.items()},
            f"Share {scores['shared_chunk_bytes']} verified chunk bytes "
            f"({scores['overlap_a_in_b']:.0%} of A, {scores['overlap_b_in_a']:.0%} of B).",
        )
        counts["relationships"] += 1
    database.connect().commit()
    return counts
