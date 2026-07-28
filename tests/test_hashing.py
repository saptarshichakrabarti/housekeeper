"""Hashing tests: quick/full hashes, empty files, duplicates, byte comparison, verification."""

import hashlib

import pytest

from housekeeper.hashing import (
    compute_full_hash,
    compute_quick_hash,
    verify_file_against_manifest,
)


def test_full_hash_matches_hashlib(tmp_path):
    target = tmp_path / "f.bin"
    payload = b"the quick brown fox" * 1000
    target.write_bytes(payload)
    result = compute_full_hash(target, "sha256", 4096)
    assert result.stable
    assert result.digest == hashlib.sha256(payload).hexdigest()
    assert result.size == len(payload)


def test_empty_file(tmp_path):
    target = tmp_path / "empty"
    target.write_bytes(b"")
    result = compute_full_hash(target, "sha256", 4096)
    assert result.stable
    assert result.digest == hashlib.sha256(b"").hexdigest()
    assert result.size == 0


def test_duplicate_files_share_full_hash(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.write_bytes(b"identical")
    b.write_bytes(b"identical")
    assert compute_full_hash(a, "sha256", 4096).digest == compute_full_hash(b, "sha256", 4096).digest


def test_same_size_different_content_differs(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.write_bytes(b"AAAA")
    b.write_bytes(b"BBBB")
    assert compute_full_hash(a, "sha256", 4096).digest != compute_full_hash(b, "sha256", 4096).digest


def test_quick_hash_is_stable_and_deterministic(tmp_path):
    target = tmp_path / "big.bin"
    target.write_bytes(bytes(range(256)) * 5000)
    first = compute_quick_hash(target, 1024, 2, "sha256")
    second = compute_quick_hash(target, 1024, 2, "sha256")
    assert first.stable and first.digest == second.digest


def test_read_error_returns_unstable(tmp_path):
    missing = tmp_path / "nope.bin"
    result = compute_full_hash(missing, "sha256", 4096)
    assert not result.stable
    assert result.digest is None
    assert result.error


def test_quick_hash_of_a_small_file_reads_it_once(tmp_path):
    """Below (samples+2) x block the sampled reads cover the file anyway — three times over."""
    target = tmp_path / "small.bin"
    payload = b"small payload"
    target.write_bytes(payload)
    result = compute_quick_hash(target, 1024, 2, "sha256")
    assert result.stable
    assert result.digest == hashlib.sha256(payload).hexdigest()


@pytest.mark.parametrize(
    "size",
    [0, 1, 999, 4096, 40_000, 300_000],
    ids=["empty", "byte", "tiny", "block", "sampled", "many-blocks"],
)
def test_single_read_identity_matches_the_two_pass_digests(tmp_path, size):
    """4.5: both digests now come from one pass, so both must be bit-identical to the old ones.

    Stored quick hashes from earlier scans are compared against freshly computed ones during
    rename detection; if these diverged, every rename would silently stop being detected.
    """
    from housekeeper.hashing import compute_identity

    target = tmp_path / "payload.bin"
    target.write_bytes(bytes((index * 7 + 3) % 256 for index in range(size)))
    chunk, samples, block = 4096, 2, 8192

    full, quick = compute_identity(target, "sha256", block, chunk, samples)
    assert full.digest == compute_full_hash(target, "sha256", block).digest
    assert quick.digest == compute_quick_hash(target, chunk, samples, "sha256").digest


def test_single_read_identity_reads_the_file_once(tmp_path):
    from housekeeper.core import counters
    from housekeeper.hashing import compute_identity

    target = tmp_path / "big.bin"
    target.write_bytes(b"x" * 300_000)
    with counters.recording() as one_pass:
        compute_identity(target, "sha256", 8192, 4096, 2)
    with counters.recording() as two_pass:
        compute_quick_hash(target, 4096, 2, "sha256")
        compute_full_hash(target, "sha256", 8192)
    assert one_pass["source_bytes_read"] == 300_000
    assert two_pass["source_bytes_read"] > one_pass["source_bytes_read"]


def test_verify_file_against_manifest_returns_on_match(tmp_path):
    target = tmp_path / "f"
    target.write_bytes(b"payload")
    digest = hashlib.sha256(b"payload").hexdigest()
    result = verify_file_against_manifest(target, len(b"payload"), digest)
    assert result.digest == digest


@pytest.mark.parametrize(
    "size,digest",
    [(999, hashlib.sha256(b"payload").hexdigest()), (7, "0" * 64)],
    ids=["wrong-size", "wrong-hash"],
)
def test_verify_file_against_manifest_raises_on_mismatch(tmp_path, size, digest):
    """The whole point: a mismatch must be impossible to ignore."""
    target = tmp_path / "f"
    target.write_bytes(b"payload")
    with pytest.raises(ValueError, match="mismatch"):
        verify_file_against_manifest(target, size, digest)
