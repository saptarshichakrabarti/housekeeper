"""Content-defined chunking analyzer: partial-content overlap (Tier-4).

Selective and opt-in: only content objects at/above ``chunking.minimum_file_size_bytes`` are
chunked. Candidate pairs come from an inverted chunk index (never all-pairs). Overlap produces
``PARTIAL_CONTENT_OVERLAP`` / ``NEAR_SUBSET_CONTENT`` relationships at Tier 4 — never exact.
"""

from __future__ import annotations

from pathlib import Path

from ..chunking.index import store_chunks
from ..chunking.overlap import compute_overlap, generate_overlap_candidates
from ..chunking.profiles import get_or_create_chunk_profile_id, profile_from_config
from ..chunking.python_backend import chunk_file
from ..hashing import compute_full_hash
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


def _ensure_hashed_large_files(database, config, minimum_file: int, allowed_entries) -> None:
    algorithm = config.section("hashing")["algorithm"]
    block = config.section("hashing")["full_hash_block_bytes"]
    for row in database.iter_rows(
        """SELECT e.id,e.absolute_path,e.size_bytes FROM filesystem_entries e
           LEFT JOIN entry_content_links l ON l.entry_id=e.id
           WHERE e.entry_type='file' AND l.entry_id IS NULL AND e.size_bytes>=?""",
        (minimum_file,),
    ):
        if allowed_entries is not None and int(row["id"]) not in allowed_entries:
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


def run_chunk_analysis(database, config, scope=None, job_id=None) -> dict[str, int]:
    from ..jobs import check_cancelled, update_job

    minimum_file = int(config.section("chunking")["minimum_file_size_bytes"])
    profile = profile_from_config(config)
    profile_id = get_or_create_chunk_profile_id(database, profile)
    allowed_entries = None
    if scope is not None:
        from .scope import scoped_entry_ids

        allowed_entries = scoped_entry_ids(database, scope)
    _ensure_hashed_large_files(database, config, minimum_file, allowed_entries)
    counts = {"chunked": 0, "chunks": 0, "skipped": 0}
    objects = database.fetch_all(
        "SELECT id FROM content_objects WHERE size_bytes>=? ORDER BY id", (minimum_file,)
    )
    for index, obj in enumerate(objects, start=1):
        if job_id:
            check_cancelled(database, job_id)
        path = _representative_path(database, int(obj["id"]))
        if path is None:
            counts["skipped"] += 1
            continue
        records = list(chunk_file(path, profile))
        store_chunks(database, int(obj["id"]), profile_id, profile, records)
        counts["chunked"] += 1
        counts["chunks"] += len(records)
        if job_id:
            update_job(database, job_id, "RUNNING", processed_count=index)
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
