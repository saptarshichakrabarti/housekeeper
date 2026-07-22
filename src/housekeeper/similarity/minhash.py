"""Deterministic MinHash signatures (dependency-free) and index maintenance."""

from __future__ import annotations

import hashlib
import random

_PRIME = (1 << 61) - 1
_MAXHASH = (1 << 64) - 1
SIGNATURE_VERSION = "1"


def _permutations(num_perm: int, seed: int = 0xA5A5) -> list[tuple[int, int]]:
    rng = random.Random(seed)
    return [(rng.randrange(1, _PRIME), rng.randrange(0, _PRIME)) for _ in range(num_perm)]


def _shingle_hash(shingle: str) -> int:
    return int.from_bytes(hashlib.blake2b(shingle.encode("utf-8"), digest_size=8).digest(), "big")


def minhash_signature(shingles: set[str], num_perm: int = 128) -> list[int]:
    params = _permutations(num_perm)
    signature = [_MAXHASH] * num_perm
    for shingle in shingles:
        x = _shingle_hash(shingle)
        for i, (a, b) in enumerate(params):
            value = (a * x + b) % _PRIME
            if value < signature[i]:
                signature[i] = value
    return signature


def estimated_jaccard(sig_a: list[int], sig_b: list[int]) -> float:
    if not sig_a or len(sig_a) != len(sig_b):
        return 0.0
    return sum(1 for x, y in zip(sig_a, sig_b) if x == y) / len(sig_a)


def clear_minhash_index(database, dry_run: bool = True) -> dict[str, int]:
    """Remove derived MinHash signatures only; never touches source files or raw hashes."""
    count = database.fetch_one(
        "SELECT COUNT(*) n FROM similarity_signatures WHERE signature_type='TEXT_MINHASH'"
    )["n"]
    result = {"text_minhash_signatures": int(count), "dry_run": int(dry_run)}
    if not dry_run:
        database.connect().execute(
            "DELETE FROM similarity_signatures WHERE signature_type='TEXT_MINHASH'"
        )
        database.connect().commit()
    return result
