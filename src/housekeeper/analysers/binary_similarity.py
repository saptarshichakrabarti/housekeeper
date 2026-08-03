"""Binary fuzzy-similarity analyser (TLSH / ssdeep), capability-gated and bucketed.

Optional: missing backends report availability and no-op. Digests bucketed by type + size
band (never all-pairs); matches are Tier-5 candidates only — never an exact classification.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from ..config import performance_profile
from ..core.worker_pool import bounded_map
from ..relationships import upsert_content_relationship
from ..similarity.fuzzy_hashes import capabilities, tlsh_digest, tlsh_distance

ALGORITHM = "binary_tlsh"
ALGORITHM_VERSION = "1"
_TLSH_MAX_DISTANCE = 60  # smaller = more similar


def _size_band(size: int) -> int:
    band = 0
    while size > 1024:
        size //= 2
        band += 1
    return band


def run_binary_similarity_analysis(database, config, job_id=None) -> dict:
    caps = capabilities()
    section = config.section("binary_similarity")
    if not caps["TLSH_AVAILABLE"] or not section.get("tlsh_enabled", False):
        # Honest capability report; no relationships are ever fabricated without a real backend.
        return {
            "status": "unavailable",
            **caps,
            "note": "install 'tlsh' and set binary_similarity.tlsh_enabled to enable binary fuzzy similarity",
            "relationships": 0,
        }
    minimum = int(section["minimum_file_size_bytes"])
    maximum = int(section["maximum_file_size_bytes"])
    workers = int(performance_profile(config)["full_hash_workers"])

    def _digest(item: tuple[int, int, str, str]) -> tuple[tuple[str, int], int, str] | None:
        """Digest one representative off the caller thread. Reads and TLSH both release the GIL,
        so this is where the wall-clock is, and it parallelises cleanly; no database is touched."""
        cid, size, path_str, suffix = item
        path = Path(path_str)
        if not path.is_file() or path.is_symlink():
            return None
        digest = tlsh_digest(path)
        if digest is None:
            return None
        return ((suffix, _size_band(size)), cid, digest)

    # Plain tuples cross the thread boundary — never a sqlite Row. The read streams on this thread.
    candidates = (
        (int(row["cid"]), int(row["size_bytes"]), row["absolute_path"], (row["suffix"] or "").lower())
        for row in database.iter_rows(
            """SELECT co.id AS cid, co.size_bytes, e.absolute_path, e.suffix FROM content_objects co
               JOIN entry_content_links l ON l.content_object_id=co.id
               JOIN filesystem_entries e ON e.id=l.entry_id
               WHERE co.size_bytes BETWEEN ? AND ? GROUP BY co.id""",
            (minimum, maximum),
        )
    )
    # One representative entry per content object, bucketed by (suffix, size band).
    buckets: dict[tuple[str, int], list[tuple[int, str]]] = defaultdict(list)
    for result in bounded_map(_digest, candidates, workers, max(1, workers) * 4):
        if result is None:
            continue
        key, cid, digest = result
        buckets[key].append((cid, digest))
    from ..jobs import checkpoint

    signatures = 0
    relationships = 0
    for bucket_index, members in enumerate(buckets.values(), 1):
        checkpoint(database, job_id, processed_count=bucket_index, state={"buckets_done": bucket_index})
        signatures += len(members)
        # Sorted so the pairwise (a_id, b_id) orientation and write order are identical regardless of
        # the order digests completed in the pool — the stage's output stays byte-for-byte determinate.
        members = sorted(members)
        for i, (a_id, a_digest) in enumerate(members):
            for b_id, b_digest in members[i + 1 :]:
                distance = tlsh_distance(a_digest, b_digest)
                if distance is None or distance > _TLSH_MAX_DISTANCE:
                    continue
                confidence = max(0.0, 1.0 - distance / 100.0)
                upsert_content_relationship(
                    database,
                    "CONTENT_OBJECT",
                    a_id,
                    "CONTENT_OBJECT",
                    b_id,
                    "SEMANTICALLY_SIMILAR",
                    "TIER_5_PROBABILISTIC_SIMILARITY",
                    confidence,
                    ALGORITHM,
                    ALGORITHM_VERSION,
                    "1",
                    {"tlsh_distance": distance},
                    f"TLSH distance {distance} (candidate only; verify before any action).",
                )
                relationships += 1
    # This analyser is a stage: the write primitives no longer commit per row, so the one
    # commit that makes its work durable belongs here.
    database.connect().commit()
    return {"status": "ok", **caps, "signatures": signatures, "relationships": relationships}
