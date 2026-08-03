"""Review-mover safety tests: verified movement, collisions, last-copy protection."""

import json
from dataclasses import replace

import pytest

from housekeeper.analysers.exact_duplicates import run_exact_duplicate_analysis
from housekeeper.hashing import compute_full_hash
from housekeeper.models import ManifestEntry
from housekeeper.review_mover import move_approved_entries, validate_review_root
from housekeeper.scanner import DriveScanner


def _entries(database, approved_ids):
    rows = database.fetch_all(
        "SELECT e.id,e.absolute_path,e.relative_path,e.size_bytes,s.full_hash,s.hash_algorithm FROM filesystem_entries e JOIN file_signatures s ON s.entry_id=e.id WHERE e.entry_type='file' ORDER BY e.id"
    )
    return [
        ManifestEntry(
            row["id"] in approved_ids, row["id"], row["absolute_path"], row["relative_path"],
            row["size_bytes"], row["full_hash"], "REVIEW_SAFE", 1.0, [], "",
            None, "", row["hash_algorithm"],
        )
        for row in rows
    ]


def _triplicate(config, database, tmp_path):
    """Three identical copies so one can be moved while two verified copies remain."""
    root = tmp_path / "src"
    root.mkdir()
    for name in ("a.bin", "b.bin", "c.bin"):
        (root / name).write_bytes(b"identical-payload")
    DriveScanner(database, config).scan(root, incremental=False)
    run_exact_duplicate_analysis(database, config)
    return root


def test_validate_review_root_rejects_nesting(tmp_path):
    with pytest.raises(ValueError):
        validate_review_root(tmp_path / "src" / "review", tmp_path / "src")
    with pytest.raises(ValueError):
        validate_review_root(tmp_path / "outer", tmp_path / "outer" / "inner")


def test_move_requires_yes(config, database, tmp_path):
    _triplicate(config, database, tmp_path)
    noncanonical = database.fetch_one(
        "SELECT entry_id FROM exact_duplicate_members WHERE is_canonical=0 LIMIT 1"
    )["entry_id"]
    entries = _entries(database, {noncanonical})
    with pytest.raises(ValueError, match="--yes"):
        move_approved_entries(entries, tmp_path / "review", database, dry_run=False, yes=False)


def test_dry_run_moves_nothing(config, database, tmp_path):
    _triplicate(config, database, tmp_path)
    noncanonical = database.fetch_one(
        "SELECT entry_id,e.absolute_path FROM exact_duplicate_members m JOIN filesystem_entries e ON e.id=m.entry_id WHERE is_canonical=0 LIMIT 1"
    )
    entries = _entries(database, {noncanonical["entry_id"]})
    move_approved_entries(entries, tmp_path / "review", database, dry_run=True, yes=False)
    from pathlib import Path

    assert Path(noncanonical["absolute_path"]).exists()  # source untouched by a dry run
    assert database.fetch_one("SELECT status FROM move_transactions LIMIT 1")["status"] == "DRY_RUN"


def test_verified_move_and_transaction(config, database, tmp_path):
    from pathlib import Path

    _triplicate(config, database, tmp_path)
    member = database.fetch_one(
        "SELECT m.entry_id,e.absolute_path,e.relative_path FROM exact_duplicate_members m JOIN filesystem_entries e ON e.id=m.entry_id WHERE is_canonical=0 LIMIT 1"
    )
    source_path = Path(member["absolute_path"])
    expected = compute_full_hash(source_path, "sha256", 8_388_608).digest
    entries = _entries(database, {member["entry_id"]})
    review_root = tmp_path / "review"
    tx = move_approved_entries(entries, review_root, database, dry_run=False, yes=True)
    assert not source_path.exists()  # source removed after verified copy
    destination = review_root / member["relative_path"]
    assert destination.exists()
    assert compute_full_hash(destination, "sha256", 8_388_608).digest == expected
    assert tx.exists()  # durable transaction manifest
    assert database.fetch_one("SELECT status FROM move_transactions WHERE status='MOVED'")


def test_refuses_to_move_last_verified_copy(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.bin").write_bytes(b"only-two-copies")
    (root / "b.bin").write_bytes(b"only-two-copies")
    DriveScanner(database, config).scan(root, incremental=False)
    run_exact_duplicate_analysis(database, config)
    # Approving BOTH members must be refused so a verified copy always survives.
    entries = _entries(database, {e["id"] for e in database.fetch_all("SELECT id FROM filesystem_entries WHERE entry_type='file'")})
    with pytest.raises(ValueError, match="last verified copy"):
        move_approved_entries(entries, tmp_path / "review", database, dry_run=True, yes=False)


def test_destination_collision_is_not_overwritten(config, database, tmp_path):

    _triplicate(config, database, tmp_path)
    member = database.fetch_one(
        "SELECT m.entry_id,e.relative_path FROM exact_duplicate_members m JOIN filesystem_entries e ON e.id=m.entry_id WHERE is_canonical=0 LIMIT 1"
    )
    review_root = tmp_path / "review"
    collision = review_root / member["relative_path"]
    collision.parent.mkdir(parents=True)
    collision.write_bytes(b"pre-existing different content")
    entries = _entries(database, {member["entry_id"]})
    move_approved_entries(entries, review_root, database, dry_run=False, yes=True)
    # The pre-existing file must remain, and the move must be recorded as FAILED.
    assert collision.read_bytes() == b"pre-existing different content"
    assert database.fetch_one("SELECT status FROM move_transactions WHERE status='FAILED'")


def test_a_sha256_workspace_still_moves_after_the_default_changed(config, database, tmp_path):
    """The migration's whole point: an existing SHA-256 workspace keeps working end to end.

    The workspace is inventoried under SHA-256, the manifest declares SHA-256, and every
    verification — preflight, pre-move re-hash, destination check — must use SHA-256 even though
    the configured default is now BLAKE3.
    """
    config.data["hashing"]["algorithm"] = "sha256"
    _triplicate(config, database, tmp_path)
    assert database.fetch_one("SELECT hash_algorithm FROM file_signatures LIMIT 1")["hash_algorithm"] == "sha256"

    config.data["hashing"]["algorithm"] = "auto"  # the default preserves the live workspace
    member = database.fetch_one(
        "SELECT entry_id FROM exact_duplicate_members WHERE is_canonical=0 LIMIT 1"
    )
    entries = _entries(database, {member["entry_id"]})
    assert [e.expected_hash_algorithm for e in entries if e.approved] == ["sha256"]
    transaction = move_approved_entries(entries, tmp_path / "review", database, dry_run=False, yes=True)
    record = json.loads(transaction.read_text(encoding="utf-8").splitlines()[0])
    assert record["status"] == "MOVED"
    assert record["expected_hash_algorithm"] == "sha256"
    assert record["pre_move_hash"] == record["post_move_hash"] == record["expected_hash"]


def test_a_manifest_from_the_wrong_algorithm_is_refused(config, database, tmp_path):
    """A digest that came from another function is not weaker evidence — it is no evidence."""
    _triplicate(config, database, tmp_path)
    member = database.fetch_one(
        "SELECT entry_id FROM exact_duplicate_members WHERE is_canonical=0 LIMIT 1"
    )
    entries = _entries(database, {member["entry_id"]})
    mislabelled = [
        replace(e, expected_hash_algorithm="sha256") if e.approved else e for e in entries
    ]
    with pytest.raises(ValueError, match="hash algorithm"):
        move_approved_entries(mislabelled, tmp_path / "review", database, dry_run=True, yes=True)
