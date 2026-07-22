"""Locality-sensitive hashing over MinHash signatures for candidate generation."""

from __future__ import annotations

import hashlib
from collections import defaultdict


def choose_bands(num_perm: int, threshold: float) -> tuple[int, int]:
    """Pick (bands, rows) minimizing the LSH error at the target Jaccard threshold."""
    best = (num_perm, 1)
    best_error = 1.0
    for rows in range(1, num_perm + 1):
        if num_perm % rows:
            continue
        bands = num_perm // rows
        # Probability two items with similarity == threshold become a candidate.
        probability = 1 - (1 - threshold**rows) ** bands
        error = abs(probability - 0.5)
        if error < best_error:
            best_error = error
            best = (bands, rows)
    return best


def _band_key(band: list[int]) -> str:
    return hashlib.blake2b(
        ",".join(str(v) for v in band).encode(), digest_size=12
    ).hexdigest()


def candidate_pairs(
    signatures: dict[int, list[int]], num_perm: int, threshold: float
) -> set[tuple[int, int]]:
    """Return candidate id pairs whose signatures collide in at least one band."""
    if not signatures:
        return set()
    bands, rows = choose_bands(num_perm, threshold)
    buckets: dict[tuple[int, str], list[int]] = defaultdict(list)
    for object_id, signature in signatures.items():
        for band_index in range(bands):
            band = signature[band_index * rows : (band_index + 1) * rows]
            buckets[(band_index, _band_key(band))].append(object_id)
    pairs: set[tuple[int, int]] = set()
    for members in buckets.values():
        unique = sorted(set(members))
        if not 2 <= len(unique) <= 512:
            continue
        for i, a in enumerate(unique):
            for b in unique[i + 1 :]:
                pairs.add((a, b))
    return pairs
