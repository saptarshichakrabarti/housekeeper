"""Incremental scanning: every scan state is recorded, plus rename/missing/reappear evidence."""

import json

from housekeeper.constants import EntryType
from housekeeper.models import FileStatRecord
from housekeeper.scanner import DriveScanner


def _changes(database, run_id):
    return {
        r["relative_path"]: r["change_status"]
        for r in database.fetch_all(
            "SELECT relative_path,change_status FROM scan_entry_changes WHERE scan_run_id=?",
            (run_id,),
        )
    }


def _latest_run(database):
    return database.fetch_one("SELECT MAX(id) AS m FROM scan_runs")["m"]


def test_all_scan_states_recorded(config, database, tmp_path):
    root = tmp_path / "src"
    sub = root / "sub"
    sub.mkdir(parents=True)
    (root / "unchanged.txt").write_text("stable", encoding="utf-8")
    (root / "willchange.txt").write_text("before", encoding="utf-8")
    (root / "willgo.txt").write_text("temporary", encoding="utf-8")
    scanner = DriveScanner(database, config)
    scanner.scan(root, incremental=True)

    # Mutate: change content, add a new file (also changing 'sub' dir mtime), remove one file.
    (root / "willchange.txt").write_text("after-and-longer", encoding="utf-8")
    (sub / "brandnew.txt").write_text("new", encoding="utf-8")
    (root / "willgo.txt").unlink()
    scanner.scan(root, incremental=True)

    changes = _changes(database, _latest_run(database))
    assert changes["unchanged.txt"] == "UNCHANGED"
    assert changes["willchange.txt"] == "CONTENT_POSSIBLY_CHANGED"
    assert changes["sub/brandnew.txt"] == "NEW"
    assert changes["willgo.txt"] == "MISSING"
    # Adding a child changes the directory's mtime -> the directory is METADATA_CHANGED.
    assert changes["sub"] == "METADATA_CHANGED"


def test_rename_is_a_moved_candidate(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "original.bin").write_bytes(b"stable rename payload")
    scanner = DriveScanner(database, config)
    scanner.scan(root, incremental=True)
    (root / "original.bin").rename(root / "renamed.bin")
    scanner.scan(root, incremental=True)
    changes = _changes(database, _latest_run(database))
    assert changes["renamed.bin"] == "MOVED_OR_RENAMED_CANDIDATE"
    assert changes["original.bin"] == "MISSING"


def test_error_state_recorded(config, database, tmp_path, monkeypatch):
    root = tmp_path / "src"
    root.mkdir()
    (root / "ok.txt").write_text("fine", encoding="utf-8")
    (root / "broken.txt").write_text("will error", encoding="utf-8")
    scanner = DriveScanner(database, config)
    scanner.scan(root, incremental=True)

    real_inspect = scanner.inspect_entry

    def flaky_inspect(path, scan_root):
        record = real_inspect(path, scan_root)
        if path.name == "broken.txt":
            return FileStatRecord(
                record.path, record.relative_path, record.name, EntryType.OTHER,
                read_error="simulated read failure",
            )
        return record

    monkeypatch.setattr(scanner, "inspect_entry", flaky_inspect)
    scanner.scan(root, incremental=True)
    changes = _changes(database, _latest_run(database))
    assert changes["broken.txt"] == "ERROR"


def test_reappearing_file_keeps_evidence(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    target = root / "intermittent.txt"
    target.write_text("present", encoding="utf-8")
    scanner = DriveScanner(database, config)
    scanner.scan(root, incremental=True)
    target.unlink()
    scanner.scan(root, incremental=True)
    target.write_text("present", encoding="utf-8")
    scanner.scan(root, incremental=True)
    row = database.fetch_one(
        "SELECT evidence_json FROM scan_entry_changes WHERE scan_run_id=? AND relative_path='intermittent.txt'",
        (_latest_run(database),),
    )
    assert json.loads(row["evidence_json"])["reappeared_after_missing"] is True


def test_content_analysis_reused_after_unchanged_rescan(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "doc.txt").write_text("stable document body", encoding="utf-8")
    from housekeeper.analyzers.registry import run_content_analysis

    scanner = DriveScanner(database, config)
    scanner.scan(root, incremental=True)
    run_content_analysis(database, config, "documents")
    before = database.fetch_one("SELECT COUNT(*) n FROM content_objects")["n"]
    scanner.scan(root, incremental=True)
    run_content_analysis(database, config, "documents")
    after = database.fetch_one("SELECT COUNT(*) n FROM content_objects")["n"]
    assert before == after == 1  # identical content analysed once
