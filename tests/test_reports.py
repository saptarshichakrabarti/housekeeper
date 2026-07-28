"""Static report generation: distinct per-type content, exports, escaping."""

import csv

import pytest

from housekeeper.reports.exports import export_csv, export_jsonl
from housekeeper.reports.formatting import human_size, percent
from housekeeper.reports.generator import generate_all_reports, generate_report
from housekeeper.scanner import DriveScanner
from tests.conftest import analyse_and_classify


def test_formatting_helpers():
    assert human_size(0) == "0 B"
    assert human_size(1536) == "1.5 KiB"
    assert human_size(5 * 1024**3) == "5.0 GiB"
    assert percent(1, 4) == "25.0%"
    assert percent(1, 0) == "0%"


def test_all_reports_generated_with_exports(scanned):
    database, config, _ = scanned
    analyse_and_classify(database, config)
    paths = generate_all_reports(database, config)
    names = {p.name for p in paths}
    assert "recommendations.csv" in names and "recommendations.jsonl" in names
    for html in ("summary", "inventory", "exact_duplicates", "directory_overlap",
                 "document_versions", "image_groups", "large_files", "projects", "errors"):
        assert f"{html}.html" in names


def test_reports_are_distinct_not_stubs(scanned):
    database, config, _ = scanned
    analyse_and_classify(database, config)
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
    from housekeeper.analysers.exact_duplicates import run_exact_duplicate_analysis

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
    from housekeeper.analysers.exact_duplicates import run_exact_duplicate_analysis
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


# --- reporting.redact_source_paths ---------------------------------------------------------------
# The key this replaces, `redact_source_root_in_reports`, was read by nothing: reports always
# contained full absolute paths regardless of the setting. It was deleted rather than wired, on the
# grounds that redaction is a feature with a threat-model question. That question has an answer —
# reports are static HTML that gets copied and shared, and an absolute path carries the account name
# and directory layout of the machine that produced it — so here is the feature, with its tests.


def _report_text(database, config, name="large_files"):
    from housekeeper.reports.generator import generate_report

    return generate_report(name, database, config).read_text(encoding="utf-8")


@pytest.fixture
def scanned_with_a_large_file(config, database, tmp_path):
    from housekeeper.policies import classify_all_entries
    from housekeeper.scanner import DriveScanner

    root = tmp_path / "private-drive"
    root.mkdir()
    (root / "holiday.txt").write_text("x" * 4096, encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    classify_all_entries(database, config)
    config.section("reporting")["large_file_threshold_bytes"] = 1
    return database, config, root


def test_reports_contain_the_mount_path_by_default(scanned_with_a_large_file):
    """What the HTML actually leaks is the source root, not a path per file.

    Worth stating precisely, because the deleted config key was named
    `redact_source_root_in_reports` and the per-file tables render `relative_path` already. The
    absolute paths live in the CSV/JSONL exports, covered separately below.
    """
    database, config, root = scanned_with_a_large_file
    assert str(root) in _report_text(database, config, "summary")


def test_redaction_removes_the_mount_path_but_keeps_the_relative_one(scanned_with_a_large_file):
    database, config, root = scanned_with_a_large_file
    config.section("reporting")["redact_source_paths"] = True
    summary = _report_text(database, config, "summary")
    assert str(root) not in summary, "the mount path survived redaction"
    assert "&lt;source&gt;" in summary, summary[:600]
    # The drive is still identified, by fingerprint rather than by where it was mounted.
    fingerprint = database.fetch_one("SELECT source_root_fingerprint f FROM scan_runs")["f"]
    assert fingerprint[:16] in summary

    large = _report_text(database, config, "large_files")
    assert "holiday.txt" in large, "redaction must not remove the source-relative path"


def test_redaction_covers_the_summary_and_inventory_reports(scanned_with_a_large_file):
    database, config, root = scanned_with_a_large_file
    config.section("reporting")["redact_source_paths"] = True
    for name in ("summary", "inventory", "exact_duplicates"):
        assert str(root) not in _report_text(database, config, name), f"{name} leaked the mount path"


def test_redaction_covers_the_recommendation_exports(scanned_with_a_large_file, tmp_path):
    from housekeeper.reports.exports import export_csv, export_jsonl

    database, config, root = scanned_with_a_large_file
    database.connect().execute("UPDATE classifications SET classification='REVIEW_SAFE'")
    database.connect().commit()
    plain = export_csv(database, tmp_path / "plain.csv", config).read_text(encoding="utf-8")
    assert str(root) in plain

    config.section("reporting")["redact_source_paths"] = True
    for path, exporter in ((tmp_path / "r.csv", export_csv), (tmp_path / "r.jsonl", export_jsonl)):
        text = exporter(database, path, config).read_text(encoding="utf-8")
        assert str(root) not in text, f"{path.suffix} export leaked the mount path"
        assert "holiday.txt" in text


def test_review_manifests_are_never_redacted(scanned_with_a_large_file, tmp_path):
    """The manifest is the movement contract, so it keeps real paths even with redaction on.

    ``move-to-review`` revalidates each row by absolute path and hash immediately before touching a
    file. A redacted manifest would fail that check — or worse, be 'fixed' by someone pasting a
    guessed path back in. Redaction protects a document meant for reading; the manifest is a document
    meant for executing.
    """
    from housekeeper.manifests import export_review_manifest

    database, config, root = scanned_with_a_large_file
    config.section("reporting")["redact_source_paths"] = True
    database.connect().execute("UPDATE classifications SET classification='REVIEW_SAFE'")
    database.connect().commit()
    manifest = export_review_manifest(database, tmp_path / "review.csv", {"REVIEW_SAFE"})
    assert str(root) in manifest.read_text(encoding="utf-8")
