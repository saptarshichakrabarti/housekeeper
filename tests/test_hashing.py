"""Hashing tests: quick/full hashes, empty files, duplicates, byte comparison, verification."""

import hashlib

from housekeeper.hashing import (
    compare_files_bytewise,
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


def test_bytewise_comparison(tmp_path):
    a = tmp_path / "a"
    b = tmp_path / "b"
    c = tmp_path / "c"
    a.write_bytes(b"same-bytes")
    b.write_bytes(b"same-bytes")
    c.write_bytes(b"other-bytes")
    assert compare_files_bytewise(a, b) is True
    assert compare_files_bytewise(a, c) is False


def test_verify_file_against_manifest(tmp_path):
    target = tmp_path / "f"
    target.write_bytes(b"payload")
    digest = hashlib.sha256(b"payload").hexdigest()
    result = verify_file_against_manifest(target, len(b"payload"), digest)
    assert result.digest == digest
