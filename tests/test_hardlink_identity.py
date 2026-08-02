"""Hard-link identity reuse: the same physical bytes are read once, not once per path.

This is the headline case for a snapshot-style backup drive, where ten retained snapshots are ten
hard links to one inode. The counter under test is *bytes read*, so it proves the shared data was
read a single time — not merely that the answer came out right.
"""

from __future__ import annotations

import os

import pytest

from housekeeper.analysers.exact_duplicates import run_exact_duplicate_analysis
from housekeeper.core import counters
from housekeeper.core.identity import ensure_content_identity
from housekeeper.scanner import DriveScanner


def _hardlink_or_skip(target, link):
    try:
        os.link(target, link)
    except OSError as exc:  # a filesystem without hard links (rare in CI, possible on odd mounts)
        pytest.skip(f"hard links unsupported here: {exc}")


def _all_unlinked(database):
    return database.reader().fetch_all(
        """SELECT e.id,e.scan_run_id,e.absolute_path,e.size_bytes,e.device_id,e.inode_or_file_id,e.nlink
           FROM filesystem_entries e LEFT JOIN entry_content_links l ON l.entry_id=e.id
           WHERE e.entry_type='file' AND l.entry_id IS NULL
           ORDER BY e.device_id,e.inode_or_file_id,e.id"""
    )


def test_nlink_is_captured_by_the_scan(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    body = "shared body of a backed-up file\n" * 50
    (root / "original.dat").write_text(body)
    _hardlink_or_skip(root / "original.dat", root / "snapshot1.dat")
    DriveScanner(database, config).scan(root, incremental=False)
    links = {
        row["name"]: row["nlink"]
        for row in database.fetch_all("SELECT name,nlink FROM filesystem_entries WHERE entry_type='file'")
    }
    assert links["original.dat"] == 2
    assert links["snapshot1.dat"] == 2


def test_hardlinked_copies_are_read_once(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    body = ("a distinct backed-up payload\n" * 200)
    (root / "original.dat").write_text(body)
    for i in range(1, 5):  # five paths, one inode — a five-snapshot backup of one file
        _hardlink_or_skip(root / "original.dat", root / f"snapshot{i}.dat")
    DriveScanner(database, config).scan(root, incremental=False)

    with counters.recording() as counts:
        result = ensure_content_identity(database, config, _all_unlinked(database))

    one_copy = len(body.encode())
    assert counts["full_hash_bytes"] == one_copy, "the shared inode was read more than once"
    assert result["hashed"] == 1
    assert result["reused"] == 4
    # All five paths resolve to the one content object.
    assert database.fetch_one(
        """SELECT COUNT(DISTINCT l.content_object_id) n FROM entry_content_links l
           JOIN filesystem_entries e ON e.id=l.entry_id WHERE e.entry_type='file'"""
    )["n"] == 1
    assert database.fetch_one(
        "SELECT COUNT(*) n FROM entry_content_links l JOIN filesystem_entries e ON e.id=l.entry_id WHERE e.entry_type='file'"
    )["n"] == 5


def test_reuse_can_be_switched_off(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    body = "payload read once per path when reuse is off\n" * 100
    (root / "original.dat").write_text(body)
    for i in range(1, 4):
        _hardlink_or_skip(root / "original.dat", root / f"copy{i}.dat")
    DriveScanner(database, config).scan(root, incremental=False)

    config.section("hashing")["hardlink_identity_reuse"] = False
    with counters.recording() as counts:
        result = ensure_content_identity(database, config, _all_unlinked(database))

    assert counts["full_hash_bytes"] == len(body.encode()) * 4, "reuse was off; every path should read"
    assert result["hashed"] == 4
    assert result["reused"] == 0
    # Off or on, the answer is the same: all four paths still resolve to one content object.
    assert database.fetch_one(
        """SELECT COUNT(DISTINCT l.content_object_id) n FROM entry_content_links l
           JOIN filesystem_entries e ON e.id=l.entry_id WHERE e.entry_type='file'"""
    )["n"] == 1


def test_hardlinked_backups_still_group_as_duplicates(config, database, tmp_path):
    """Reuse must not hide that the copies exist: they are still verified exact duplicates."""
    root = tmp_path / "src"
    root.mkdir()
    body = "a file kept in three snapshot trees\n" * 80
    for snapshot in ("snap_a", "snap_b", "snap_c"):
        (root / snapshot).mkdir()
    (root / "snap_a" / "doc.dat").write_text(body)
    _hardlink_or_skip(root / "snap_a" / "doc.dat", root / "snap_b" / "doc.dat")
    _hardlink_or_skip(root / "snap_a" / "doc.dat", root / "snap_c" / "doc.dat")
    DriveScanner(database, config).scan(root, incremental=False)

    run_exact_duplicate_analysis(database, config)
    group = database.fetch_one(
        "SELECT member_count FROM exact_duplicate_groups ORDER BY member_count DESC LIMIT 1"
    )
    assert group is not None and group["member_count"] == 3
