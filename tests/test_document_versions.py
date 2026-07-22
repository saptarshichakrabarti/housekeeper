"""Document-version grouping: token retention, filename normalization, similarity, review-only."""

from housekeeper.analysers.document_versions import (
    calculate_filename_similarity,
    extract_version_tokens,
    normalize_version_filename,
    run_document_version_analysis,
)
from housekeeper.scanner import DriveScanner


def test_version_tokens_are_extracted():
    tokens = extract_version_tokens("thesis_final_v2 (1).docx")
    assert "final" in tokens
    assert "v2" in tokens


def test_normalize_strips_version_tokens_but_keeps_stem():
    assert normalize_version_filename("report_final_v2.docx").startswith("report")


def test_filename_similarity_high_for_related_versions():
    score = calculate_filename_similarity("report_draft.docx", "report_final.docx")
    assert score >= 0.5


def test_filename_similarity_low_for_unrelated():
    assert calculate_filename_similarity("taxes_2019.docx", "vacation_photos.docx") < 0.5


def test_version_family_is_grouped_and_review_only(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "essay_draft.txt").write_text("chapter one draft version", encoding="utf-8")
    (root / "essay_final.txt").write_text("chapter one final version edited", encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    from housekeeper.analysers.registry import run_content_analysis

    run_content_analysis(database, config, "documents")
    run_document_version_analysis(database, config)
    # The two files have different content, so they must never be an exact-duplicate group.
    assert database.fetch_one("SELECT COUNT(*) n FROM exact_duplicate_groups")["n"] == 0
    # Version relationships are review-only by type (LIKELY_VERSION_OF), never EXACT_DUPLICATE.
    versions = database.fetch_all(
        "SELECT * FROM relationships WHERE relationship_type='LIKELY_VERSION_OF'"
    )
    assert versions, "expected a likely-version relationship between the two essay files"
    for relation in versions:
        assert relation["relationship_type"] == "LIKELY_VERSION_OF"
