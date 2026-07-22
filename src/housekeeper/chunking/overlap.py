"""Partial-overlap candidate generation (inverted chunk index) and exact overlap scoring."""

from __future__ import annotations

from collections import defaultdict


def generate_overlap_candidates(
    database, profile_id: int, common_chunk_cutoff: int
) -> set[tuple[int, int]]:
    """Content objects sharing at least one non-common chunk become candidate pairs.

    Extremely common chunks (occurrence_count above the cutoff) are treated as non-discriminating
    "stop chunks" and skipped, keeping the pairwise fan-out bounded.
    """
    chunk_to_objects: dict[int, list[int]] = defaultdict(list)
    for row in database.iter_rows(
        """SELECT o.chunk_id, o.content_object_id FROM chunk_occurrences o
           JOIN content_chunks c ON c.id=o.chunk_id
           WHERE c.chunking_profile_id=? AND c.occurrence_count<=?""",
        (profile_id, common_chunk_cutoff),
    ):
        chunk_to_objects[int(row["chunk_id"])].append(int(row["content_object_id"]))
    pairs: set[tuple[int, int]] = set()
    for objects in chunk_to_objects.values():
        unique = sorted(set(objects))
        if not 2 <= len(unique) <= 256:
            continue
        for i, a in enumerate(unique):
            for b in unique[i + 1 :]:
                pairs.add((a, b))
    return pairs


def _chunk_map(database, content_object_id: int) -> dict[int, int]:
    """chunk_id -> size for a content object (a chunk id repeats only if content repeats)."""
    sizes: dict[int, int] = {}
    for row in database.iter_rows(
        "SELECT chunk_id,size_bytes FROM chunk_occurrences WHERE content_object_id=?",
        (content_object_id,),
    ):
        sizes[int(row["chunk_id"])] = int(row["size_bytes"])
    return sizes


def compute_overlap(database, a_id: int, b_id: int) -> dict[str, float | int]:
    a = _chunk_map(database, a_id)
    b = _chunk_map(database, b_id)
    shared_ids = set(a) & set(b)
    shared_bytes = sum(a[c] for c in shared_ids)
    a_total = sum(a.values())
    b_total = sum(b.values())
    union_bytes = a_total + b_total - shared_bytes
    return {
        "shared_chunk_count": len(shared_ids),
        "shared_chunk_bytes": shared_bytes,
        "a_total_chunk_bytes": a_total,
        "b_total_chunk_bytes": b_total,
        "overlap_a_in_b": shared_bytes / a_total if a_total else 0.0,
        "overlap_b_in_a": shared_bytes / b_total if b_total else 0.0,
        "weighted_jaccard": shared_bytes / union_bytes if union_bytes else 0.0,
    }
