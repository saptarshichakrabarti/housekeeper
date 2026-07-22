"""Scanner tests: traversal, hidden files, symlink no-follow, cycles, exclusions, resume."""

from housekeeper.constants import EntryType
from housekeeper.scanner import DriveScanner, build_source_root_fingerprint


def test_normal_traversal_counts(config, database, tmp_path):
    root = tmp_path / "src"
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("a", encoding="utf-8")
    (root / "sub" / "b.txt").write_text("bb", encoding="utf-8")
    counts = DriveScanner(database, config).scan(root, incremental=False)
    assert counts["files"] == 2
    assert counts["dirs"] == 1
    assert counts["bytes"] == 3


def test_hidden_files_are_recorded(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / ".secret").write_text("x", encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    row = database.fetch_one("SELECT is_hidden FROM filesystem_entries WHERE name='.secret'")
    assert row["is_hidden"] == 1


def test_symlinks_recorded_not_followed(config, database, tmp_path):
    root = tmp_path / "src"
    (root / "real").mkdir(parents=True)
    (root / "real" / "f.txt").write_text("hi", encoding="utf-8")
    (root / "link").symlink_to(root / "real", target_is_directory=True)
    counts = DriveScanner(database, config).scan(root, incremental=False)
    assert counts["symlinks"] == 1
    link = database.fetch_one("SELECT entry_type,symlink_target FROM filesystem_entries WHERE name='link'")
    assert link["entry_type"] == EntryType.SYMLINK
    # The symlink target's files must not be traversed through the link.
    assert database.fetch_one("SELECT COUNT(*) n FROM filesystem_entries WHERE relative_path LIKE 'link/%'")["n"] == 0


def test_symlink_cycle_terminates(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "self").symlink_to(root, target_is_directory=True)
    # Must return without recursing into the self-referential link.
    counts = DriveScanner(database, config).scan(root, incremental=False)
    assert counts["symlinks"] == 1


def test_exclusions(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "keep.txt").write_text("k", encoding="utf-8")
    (root / "skip.log").write_text("s", encoding="utf-8")
    config.section("scanner")["excluded_names"] = ["skip.log"]
    DriveScanner(database, config).scan(root, incremental=False)
    assert database.fetch_one("SELECT COUNT(*) n FROM filesystem_entries WHERE name='skip.log'")["n"] == 0
    assert database.fetch_one("SELECT COUNT(*) n FROM filesystem_entries WHERE name='keep.txt'")["n"] == 1


def test_unusual_filenames(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    weird = root / "résumé — draft (2)#.txt"
    weird.write_text("unicode", encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    assert database.fetch_one("SELECT COUNT(*) n FROM filesystem_entries WHERE name=?", (weird.name,))["n"] == 1


def test_resume_reuses_incomplete_run(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.txt").write_text("a", encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    run_id = database.fetch_one("SELECT id FROM scan_runs ORDER BY id DESC LIMIT 1")["id"]
    database.connect().execute("UPDATE scan_runs SET status='INTERRUPTED' WHERE id=?", (run_id,))
    database.connect().commit()
    DriveScanner(database, config).scan(root, resume=True, incremental=False)
    # Resuming an interrupted run reuses the same scan_run id rather than creating a twin.
    assert database.fetch_one("SELECT status FROM scan_runs WHERE id=?", (run_id,))["status"] == "COMPLETE"


def test_source_root_fingerprint_is_stable(tmp_path):
    root = tmp_path / "drive"
    root.mkdir()
    assert build_source_root_fingerprint(root) == build_source_root_fingerprint(root)


def test_scanning_does_not_modify_source(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    target = root / "a.txt"
    target.write_text("content", encoding="utf-8")
    before = target.stat().st_mtime_ns
    DriveScanner(database, config).scan(root, incremental=False)
    assert target.read_text(encoding="utf-8") == "content"
    assert target.stat().st_mtime_ns == before
