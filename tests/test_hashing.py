"""Hashing tests: quick/full hashes, empty files, duplicates, byte comparison, verification."""

import copy
import hashlib
from pathlib import Path

import pytest

from housekeeper import hashing
from housekeeper.hashing import (
    compute_full_hash,
    compute_identity,
    compute_quick_hash,
    verify_file_against_manifest,
)


def test_hashing_prefers_a_compatible_rust_backend(monkeypatch, tmp_path):
    """Normal hashing uses Rust when capability detection has selected it."""
    from housekeeper import hashing

    class RustBackend:
        def full_hash(self, path, algorithm, block_size):
            assert Path(path).is_file()
            return {"full_hash": "native-full", "size_bytes": 3, "stable": True, "error": None}

        def quick_hash(self, path, algorithm, chunk_size, middle_samples):
            assert Path(path).is_file()
            return {"quick_hash": "native-quick", "size_bytes": 3, "stable": True, "error": None}

        def identity_hash(self, path, algorithm, block_size, chunk_size, middle_samples):
            assert Path(path).is_file()
            return {
                "full_hash": "native-full",
                "quick_hash": "native-quick",
                "size_bytes": 3,
                "bytes_read": 3,
                "stable": True,
                "error": None,
            }

    target = tmp_path / "native.bin"
    target.write_bytes(b"abc")
    monkeypatch.setattr(hashing._native_backends, "backend", RustBackend(), raising=False)
    assert compute_full_hash(target, "sha256", 4096).digest == "native-full"
    assert compute_quick_hash(target, 1024, 2, "sha256").digest == "native-quick"
    full, quick = compute_identity(target, "sha256", 4096, 1024, 2)
    assert (full.digest, quick.digest) == ("native-full", "native-quick")


def test_hashing_falls_back_after_a_rust_failure(monkeypatch, tmp_path):
    from housekeeper import hashing

    class BrokenRustBackend:
        def full_hash(self, *_args):
            raise RuntimeError("backend exited")

        def close(self):
            pass

    target = tmp_path / "fallback.bin"
    target.write_bytes(b"abc")
    monkeypatch.setattr(hashing._native_backends, "backend", BrokenRustBackend(), raising=False)
    assert compute_full_hash(target, "sha256", 4096).digest == hashlib.sha256(b"abc").hexdigest()


def test_cache_hygiene_never_changes_the_digest(tmp_path):
    """O_NOATIME, fadvise and DONTNEED are performance hints; the bytes hashed must be identical."""
    target = tmp_path / "f.bin"
    payload = b"cache-hygiene payload " * 5000
    target.write_bytes(payload)
    expected = hashlib.sha256(payload).hexdigest()
    assert compute_full_hash(target, "sha256", 4096).digest == expected
    full, quick = compute_identity(target, "sha256", 4096, 1024, 2, drop_cache=True)
    assert full.digest == expected
    # The quick digest stays a by-product of the same read, identical to the standalone quick hash.
    assert quick.digest == compute_quick_hash(target, 1024, 2, "sha256").digest


def test_hash_cpu_io_split_is_recorded(tmp_path):
    """The measurement that decides whether a faster hash is worth adopting must be observable."""
    from housekeeper.core import counters

    target = tmp_path / "f.bin"
    target.write_bytes(b"measure me " * 100_000)
    with counters.recording() as counts:
        compute_identity(target, "sha256", 65536, 4096, 2)
    assert "stage_ms:hash_io" in counts
    assert "stage_ms:hash_cpu" in counts


def test_blake3_is_the_default_and_the_sha_family_stays_supported():
    """blake3 is a required dependency now, so the default must always be computable."""
    from housekeeper.config import DEFAULTS, merge_configs, validate_config
    from housekeeper.hashing import new_hasher

    assert DEFAULTS["hashing"]["algorithm"] == "auto"
    validate_config(copy.deepcopy(DEFAULTS))  # no raise
    digest = new_hasher("blake3")
    digest.update(b"abc")
    assert len(digest.hexdigest()) == 64  # 256-bit, schema-compatible width
    for compatible in ("sha256", "sha512", "blake2b"):
        validate_config(merge_configs(DEFAULTS, {"hashing": {"algorithm": compatible}}))
    with pytest.raises(ValueError, match="unsupported hash algorithm"):
        validate_config(merge_configs(DEFAULTS, {"hashing": {"algorithm": "md5"}}))


def test_new_workspace_persists_blake3_for_auto(database):
    from housekeeper.hashing import workspace_hash_algorithm

    assert workspace_hash_algorithm(database, "auto") == "blake3"
    row = database.fetch_one(
        "SELECT setting_value FROM workspace_settings WHERE setting_key='hash_algorithm'"
    )
    assert row["setting_value"] == "blake3"


def test_existing_workspace_keeps_and_persists_its_own_algorithm(database):
    """A workspace already holding SHA-256 digests must not start writing BLAKE3 ones."""
    from housekeeper.hashing import workspace_hash_algorithm

    database.connect().execute(
        "INSERT INTO scan_runs(id,source_root,source_root_fingerprint,status) VALUES(1,'/r','f','COMPLETE')"
    )
    database.connect().execute(
        "INSERT INTO filesystem_entries(id,scan_run_id,source_root,absolute_path,relative_path,name,entry_type)"
        " VALUES(1,1,'/r','/r/a','a','a','file')"
    )
    database.connect().execute(
        "INSERT INTO file_signatures(entry_id,full_hash,hash_algorithm,hash_status) VALUES(1,'ab','sha256','OK')"
    )
    assert workspace_hash_algorithm(database, "auto") == "sha256"
    database.connect().execute("DELETE FROM file_signatures")
    database.connect().commit()
    assert workspace_hash_algorithm(database, "auto") == "sha256"


def test_explicit_algorithm_conflict_requires_migration(database):
    from housekeeper.hashing import workspace_hash_algorithm

    assert workspace_hash_algorithm(database, "sha256") == "sha256"
    with pytest.raises(ValueError, match="explicit re-hash migration is required"):
        workspace_hash_algorithm(database, "blake3")


def test_mixed_workspace_algorithms_fail_closed(database):
    from housekeeper.hashing import workspace_hash_algorithm

    database.connect().execute(
        "INSERT INTO content_objects(hash_algorithm,full_hash,size_bytes) VALUES"
        "('sha256','a',1),('blake3','b',2)"
    )
    with pytest.raises(ValueError, match="mixed hash algorithms"):
        workspace_hash_algorithm(database, "auto")


def test_legacy_unnamed_digest_is_migrated_as_sha256(database):
    from housekeeper.hashing import workspace_hash_algorithm

    database.connect().execute(
        "INSERT INTO scan_runs(id,source_root,source_root_fingerprint,status) VALUES(1,'/r','f','COMPLETE')"
    )
    database.connect().execute(
        "INSERT INTO filesystem_entries(id,scan_run_id,source_root,absolute_path,relative_path,name,entry_type)"
        " VALUES(1,1,'/r','/r/a','a','a','file')"
    )
    database.connect().execute(
        "INSERT INTO file_signatures(entry_id,full_hash,hash_algorithm,hash_status)"
        " VALUES(1,'legacy',NULL,'OK')"
    )
    assert workspace_hash_algorithm(database, "auto") == "sha256"


def test_persisted_algorithm_must_match_recorded_digests(database):
    from housekeeper.hashing import workspace_hash_algorithm

    database.execute(
        "INSERT INTO workspace_settings(setting_key,setting_value) VALUES('hash_algorithm','blake3')"
    )
    database.execute(
        "INSERT INTO content_objects(hash_algorithm,full_hash,size_bytes) VALUES('sha256','a',1)"
    )
    with pytest.raises(ValueError, match="persisted workspace hash algorithm"):
        workspace_hash_algorithm(database, "auto")


def test_hashing_survives_a_platform_without_fadvise(tmp_path, monkeypatch):
    """A platform (or mount) that rejects posix_fadvise must still hash correctly."""
    target = tmp_path / "f.bin"
    payload = b"no fadvise here" * 3000
    target.write_bytes(payload)

    def boom(*_args, **_kwargs):
        raise OSError("posix_fadvise unsupported")

    # Both the advice-on-open and the DONTNEED-after paths must degrade to a plain correct hash.
    monkeypatch.setattr(hashing.os, "posix_fadvise", boom, raising=False)
    result = compute_full_hash(target, "sha256", 4096)
    assert result.stable and result.digest == hashlib.sha256(payload).hexdigest()
    full, _quick = compute_identity(target, "sha256", 4096, 1024, 2, drop_cache=True)
    assert full.digest == hashlib.sha256(payload).hexdigest()


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
    result = verify_file_against_manifest(target, len(b"payload"), digest, "sha256")
    assert result.digest == digest


def test_verify_file_against_manifest_uses_the_declared_algorithm(tmp_path):
    """A BLAKE3 manifest is verified with BLAKE3; checking it with SHA-256 would reject the file."""
    import blake3

    target = tmp_path / "f"
    target.write_bytes(b"payload")
    digest = blake3.blake3(b"payload").hexdigest()
    assert verify_file_against_manifest(target, 7, digest, "blake3").digest == digest
    with pytest.raises(ValueError, match="mismatch"):
        verify_file_against_manifest(target, 7, digest, "sha256")


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
        verify_file_against_manifest(target, size, digest, "sha256")
