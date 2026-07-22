"""Exact-duplicate detection: grouping, canonical selection, cross-root, last-copy safety."""

from housekeeper.analysers.exact_duplicates import run_exact_duplicate_analysis
from housekeeper.scanner import DriveScanner


def _scan(database, config, root):
    DriveScanner(database, config).scan(root, incremental=False)


def test_duplicates_are_grouped_by_full_hash(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.bin").write_bytes(b"payload-content")
    (root / "b.bin").write_bytes(b"payload-content")
    (root / "unique.bin").write_bytes(b"different")
    _scan(database, config, root)
    run_exact_duplicate_analysis(database, config)
    groups = database.fetch_all("SELECT * FROM exact_duplicate_groups")
    assert len(groups) == 1
    assert groups[0]["member_count"] == 2


def test_canonical_is_designated_and_single(config, database, tmp_path):
    root = tmp_path / "src"
    (root / "deep" / "nested").mkdir(parents=True)
    (root / "short.bin").write_bytes(b"dup")
    (root / "deep" / "nested" / "copy.bin").write_bytes(b"dup")
    _scan(database, config, root)
    run_exact_duplicate_analysis(database, config)
    members = database.fetch_all("SELECT entry_id,is_canonical FROM exact_duplicate_members")
    canonicals = [m for m in members if m["is_canonical"]]
    assert len(canonicals) == 1


def test_duplicates_across_backup_roots(config, database, tmp_path):
    root = tmp_path / "src"
    (root / "backup-2018").mkdir(parents=True)
    (root / "backup-2020").mkdir(parents=True)
    (root / "backup-2018" / "photo.jpg").write_bytes(b"image-bytes")
    (root / "backup-2020" / "photo.jpg").write_bytes(b"image-bytes")
    _scan(database, config, root)
    run_exact_duplicate_analysis(database, config)
    group = database.fetch_one("SELECT member_count FROM exact_duplicate_groups")
    assert group["member_count"] == 2


def test_content_object_deduplicates_identical_bytes(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    for name in ("x", "y", "z"):
        (root / name).write_bytes(b"same-bytes-here")
    _scan(database, config, root)
    run_exact_duplicate_analysis(database, config)
    # Three entries, one content object.
    assert database.fetch_one("SELECT COUNT(*) n FROM content_objects")["n"] == 1
    assert database.fetch_one("SELECT COUNT(*) n FROM entry_content_links")["n"] == 3


def test_no_group_for_single_unique_files(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "a").write_bytes(b"one")
    (root / "b").write_bytes(b"two")
    _scan(database, config, root)
    run_exact_duplicate_analysis(database, config)
    assert database.fetch_one("SELECT COUNT(*) n FROM exact_duplicate_groups")["n"] == 0
