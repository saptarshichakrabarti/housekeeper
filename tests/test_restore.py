"""Restore tests: verified restoration, already-satisfied, collision, dry-run."""

import json

from housekeeper.hashing import compute_full_hash
from housekeeper.restore import restore_transaction, verify_transaction


def _transaction(tmp_path, body=b"restore-me"):
    """Simulate a completed move: file lives in review, original path is empty."""
    review = tmp_path / "review" / "a.bin"
    review.parent.mkdir(parents=True)
    review.write_bytes(body)
    original = tmp_path / "src" / "a.bin"
    original.parent.mkdir(parents=True)
    digest = compute_full_hash(review, "sha256", 8_388_608).digest
    manifest = tmp_path / "tx.jsonl"
    manifest.write_text(
        json.dumps(
            {
                "status": "MOVED",
                "source_path": str(original),
                "destination_path": str(review),
                "expected_hash": digest,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return manifest, original, review


def test_restore_moves_file_back(tmp_path):
    manifest, original, review = _transaction(tmp_path)
    results = restore_transaction(manifest, dry_run=False, yes=True)
    assert results[0]["restore_status"] == "RESTORED"
    assert original.exists()
    assert not review.exists()  # review copy removed after verified restore


def test_dry_run_restores_nothing(tmp_path):
    manifest, original, review = _transaction(tmp_path)
    results = restore_transaction(manifest, dry_run=True)
    assert results[0]["restore_status"] == "DRY_RUN"
    assert not original.exists()
    assert review.exists()


def test_already_satisfied_when_original_matches(tmp_path):
    manifest, original, review = _transaction(tmp_path)
    original.write_bytes(b"restore-me")  # identical content already present
    results = restore_transaction(manifest, dry_run=False, yes=True)
    assert results[0]["restore_status"] == "ALREADY_SATISFIED"
    assert review.exists()  # nothing removed


def test_destination_collision_stops_restore(tmp_path):
    manifest, original, _review = _transaction(tmp_path)
    original.write_bytes(b"a different file at the original path")
    results = restore_transaction(manifest, dry_run=False, yes=True)
    assert results[0]["restore_status"] == "DESTINATION_EXISTS"
    assert original.read_bytes() == b"a different file at the original path"


def test_confirmation_required_without_yes(tmp_path):
    manifest, original, _review = _transaction(tmp_path)
    results = restore_transaction(manifest, dry_run=False, yes=False)
    assert results[0]["restore_status"] == "CONFIRMATION_REQUIRED"
    assert not original.exists()


def test_verify_transaction_confirms_intact_review_copy(tmp_path):
    manifest, _original, _review = _transaction(tmp_path)
    results = verify_transaction(manifest)
    assert results[0]["verify_status"] == "VERIFIED"


def test_verify_transaction_detects_tampered_or_missing_copy(tmp_path):
    manifest, _original, review = _transaction(tmp_path)
    review.write_bytes(b"tampered content")
    assert verify_transaction(manifest)[0]["verify_status"] == "HASH_MISMATCH"
    review.unlink()
    assert verify_transaction(manifest)[0]["verify_status"] == "MISSING_DESTINATION"
