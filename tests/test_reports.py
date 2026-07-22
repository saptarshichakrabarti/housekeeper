"""Static report generation: distinct per-type content, exports, escaping."""

import csv

from housekeeper.reports.exports import export_csv, export_jsonl
from housekeeper.reports.formatting import human_size, percent
from housekeeper.reports.generator import generate_all_reports, generate_report
from housekeeper.scanner import DriveScanner
from tests.conftest import analyze_and_classify


def test_formatting_helpers():
    assert human_size(0) == "0 B"
    assert human_size(1536) == "1.5 KiB"
    assert human_size(5 * 1024**3) == "5.0 GiB"
    assert percent(1, 4) == "25.0%"
    assert percent(1, 0) == "0%"


def test_all_reports_generated_with_exports(scanned):
    database, config, _ = scanned
    analyze_and_classify(database, config)
    paths = generate_all_reports(database, config)
    names = {p.name for p in paths}
    assert "recommendations.csv" in names and "recommendations.jsonl" in names
    for html in ("summary", "inventory", "exact_duplicates", "directory_overlap",
                 "document_versions", "image_groups", "large_files", "projects", "errors"):
        assert f"{html}.html" in names


def test_reports_are_distinct_not_stubs(scanned):
    database, config, _ = scanned
    analyze_and_classify(database, config)
    generate_all_reports(database, config)
    reports = config.workspace / config.data["workspace"]["reports_dir"]
    duplicates = (reports / "exact_duplicates.html").read_text(encoding="utf-8")
    projects = (reports / "projects.html").read_text(encoding="utf-8")
    errors = (reports / "errors.html").read_text(encoding="utf-8")
    # Each report has type-specific content (the old stub produced identical bodies).
    assert "canonical" in duplicates.lower()
    assert "Generated" in projects or "Environment" in projects
    assert "parser failures" in errors.lower() or "read errors" in errors.lower()
    assert duplicates != projects != errors


def test_exact_duplicates_report_lists_group(config, database, tmp_path):
    from housekeeper.analyzers.exact_duplicates import run_exact_duplicate_analysis

    root = tmp_path / "src"
    root.mkdir()
    (root / "a.bin").write_bytes(b"payload-content")
    (root / "b.bin").write_bytes(b"payload-content")
    DriveScanner(database, config).scan(root, incremental=False)
    run_exact_duplicate_analysis(database, config)
    from housekeeper.policies import classify_all_entries

    classify_all_entries(database, config)
    path = generate_report("exact_duplicates", database, config)
    html = path.read_text(encoding="utf-8")
    assert "a.bin" in html and "b.bin" in html
    assert "redundant" in html.lower()


def test_recommendations_export_has_reason_codes(config, database, tmp_path):
    from housekeeper.analyzers.exact_duplicates import run_exact_duplicate_analysis
    from housekeeper.policies import classify_all_entries

    root = tmp_path / "src"
    root.mkdir()
    (root / "a.bin").write_bytes(b"dup")
    (root / "b.bin").write_bytes(b"dup")
    DriveScanner(database, config).scan(root, incremental=False)
    run_exact_duplicate_analysis(database, config)
    classify_all_entries(database, config)
    csv_path = export_csv(database, tmp_path / "rec.csv")
    rows = list(csv.DictReader(csv_path.open(encoding="utf-8")))
    assert rows
    assert any("EXACT_SHA256_DUPLICATE" in r["reason_codes"] for r in rows)
    jsonl = export_jsonl(database, tmp_path / "rec.jsonl").read_text(encoding="utf-8")
    assert "EXACT_SHA256_DUPLICATE" in jsonl


def test_report_html_is_escaped(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "<script>evil.txt").write_text("x", encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    from housekeeper.policies import classify_all_entries

    classify_all_entries(database, config)
    html = generate_report("inventory", database, config).read_text(encoding="utf-8")
    assert "<script>evil" not in html
    assert "&lt;script&gt;" in html
