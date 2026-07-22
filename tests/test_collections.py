"""Backup marginal value, removal simulation, and record-series tests."""

from housekeeper.analyzers.exact_duplicates import run_exact_duplicate_analysis
from housekeeper.collections.marginal_value import run_backup_value_analysis, simulate_removal
from housekeeper.collections.record_series import classify_series, run_record_series_analysis
from housekeeper.scanner import DriveScanner


def _two_backups(config, database, tmp_path):
    root = tmp_path / "src"
    newer = root / "backup-2020"
    older = root / "backup-2018"
    for base in (newer, older):
        base.mkdir(parents=True)
        (base / "shared1.txt").write_text("shared content one", encoding="utf-8")
        (base / "shared2.txt").write_text("shared content two", encoding="utf-8")
    (older / "only-old.txt").write_text("irreplaceable historical note", encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    run_exact_duplicate_analysis(database, config)  # create content objects + links
    return root


def test_backup_value_and_unique_contribution(config, database, tmp_path):
    _two_backups(config, database, tmp_path)
    result = run_backup_value_analysis(database, config)
    assert result["collections"] == 2
    import json

    clusters = {
        r["name"]: json.loads(r["summary_json"])
        for r in database.fetch_all("SELECT name,summary_json FROM collection_clusters")
    }
    old = next(v for k, v in clusters.items() if "backup-2018" in k)
    new = next(v for k, v in clusters.items() if "backup-2020" in k)
    # The older backup has a unique file; the newer one is fully redundant here.
    assert old["unique_content_objects"] == 1
    assert new["unique_content_objects"] == 0
    assert new["value_class"] == "FULLY_CONTENT_REDUNDANT_CONTEXT_REMAINS"


def test_removal_simulation_reports_unique_losses(config, database, tmp_path):
    _two_backups(config, database, tmp_path)
    run_backup_value_analysis(database, config)
    old_id = database.fetch_one(
        "SELECT id FROM collection_clusters WHERE name LIKE '%backup-2018%'"
    )["id"]
    simulation = simulate_removal(database, old_id)
    assert simulation["content_losing_all_copies"] == 1  # the unique historical note
    assert simulation["apparent_recoverable_bytes"] > 0  # shared files recoverable
    assert "SIMULATION ONLY" in simulation["note"]


def test_record_series_classification_rules():
    assert classify_series("setup.exe", ".exe", "downloads/setup.exe")[0] == "SOFTWARE_INSTALLERS"
    assert classify_series("main.py", ".py", "proj/main.py")[0] == "SOURCE_CODE"
    assert classify_series("holiday.jpg", ".jpg", "photos/holiday.jpg")[0] == "PERSONAL_PHOTOGRAPHS"
    assert classify_series("tax_2019.pdf", ".pdf", "docs/tax_2019.pdf")[0] == "FINANCIAL_AND_TAX"
    # Ambiguous defaults to UNKNOWN with low confidence (review).
    series, confidence = classify_series("mystery", "", "mystery")
    assert series == "UNKNOWN" and confidence < 0.5


def test_record_series_assignment_over_tree(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "setup.exe").write_bytes(b"MZ installer")
    (root / "notes.txt").write_text("hello", encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    counts = run_record_series_analysis(database, config)
    assert counts.get("SOFTWARE_INSTALLERS", 0) == 1
    assert database.fetch_one("SELECT COUNT(*) n FROM record_series")["n"] >= 15
    assert database.fetch_one(
        "SELECT COUNT(*) n FROM record_series_assignments WHERE target_type='ENTRY'"
    )["n"] == 2
