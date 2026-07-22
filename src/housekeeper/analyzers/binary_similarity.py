"""Binary fuzzy-similarity analyzer (TLSH / ssdeep), capability-gated and bucketed.

Optional: if neither backend is installed it reports availability and does nothing (never an
error). When available, digests are bucketed by detected type + size band (never all-pairs), and
matches emit review-only Tier-5 relationships. A fuzzy match is a candidate generator only and
must be verified before any action — it never produces an exact classification.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

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
    # One representative entry per content object, bucketed by (suffix, size band).
    buckets: dict[tuple[str, int], list[tuple[int, str]]] = defaultdict(list)
    for row in database.iter_rows(
        """SELECT co.id AS cid, co.size_bytes, e.absolute_path, e.suffix FROM content_objects co
           JOIN entry_content_links l ON l.content_object_id=co.id
           JOIN filesystem_entries e ON e.id=l.entry_id
           WHERE co.size_bytes BETWEEN ? AND ? GROUP BY co.id""",
        (minimum, maximum),
    ):
        path = Path(row["absolute_path"])
        if not path.is_file() or path.is_symlink():
            continue
        digest = tlsh_digest(path)
        if digest is None:
            continue
        buckets[((row["suffix"] or "").lower(), _size_band(int(row["size_bytes"])))].append(
            (int(row["cid"]), digest)
        )
    from ..jobs import checkpoint

    signatures = 0
    relationships = 0
    for bucket_index, members in enumerate(buckets.values(), 1):
        checkpoint(database, job_id, processed_count=bucket_index, state={"buckets_done": bucket_index})
        signatures += len(members)
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
    return {"status": "ok", **caps, "signatures": signatures, "relationships": relationships}
