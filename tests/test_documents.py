"""Document analyzer tests, including the plaintext-via-registry regression."""

import pytest

from housekeeper.analyzers.documents import (
    compute_normalized_text_hash,
    extract_document,
    normalize_document_text,
)
from housekeeper.config import load_config


@pytest.fixture
def cfg():
    return load_config()


def test_normalize_collapses_whitespace_and_nfkc():
    text = normalize_document_text("a\t\tb\n\nc\x00d", 1000)
    assert text == "a b c d"


def test_normalized_text_hash_is_stable():
    assert compute_normalized_text_hash("abc") == compute_normalized_text_hash("abc")


def test_plaintext_extraction_reachable_with_dotted_suffix(tmp_path, cfg):
    """Regression: the registry passes ``Path.suffix`` (dotted); plaintext must still parse."""
    target = tmp_path / "notes.txt"
    target.write_text("hello world from a plaintext file", encoding="utf-8")
    result = extract_document(target, ".txt", cfg)
    assert result["extraction_status"] == "OK"
    assert "plaintext" in result["normalized_text"]
    # Undotted convention still works too.
    assert extract_document(target, "txt", cfg)["extraction_status"] == "OK"


def test_markdown_and_csv_are_plaintext(tmp_path, cfg):
    md = tmp_path / "a.md"
    md.write_text("# Title\n\ncontent", encoding="utf-8")
    csv = tmp_path / "a.csv"
    csv.write_text("h1,h2\n1,2\n", encoding="utf-8")
    assert extract_document(md, ".md", cfg)["extraction_status"] == "OK"
    assert extract_document(csv, ".csv", cfg)["extraction_status"] == "OK"


def test_malformed_docx_is_not_ok(tmp_path, cfg):
    pytest.importorskip("docx")
    target = tmp_path / "broken.docx"
    target.write_bytes(b"not a real docx")
    result = extract_document(target, ".docx", cfg)
    assert result["extraction_status"] in {"ERROR", "UNSUPPORTED"}


def test_xlsx_extraction(tmp_path, cfg):
    pytest.importorskip("openpyxl")
    from openpyxl import Workbook

    path = tmp_path / "sheet.xlsx"
    workbook = Workbook()
    workbook.active.append(["marker", 7])
    workbook.save(path)
    result = extract_document(path, ".xlsx", cfg)
    assert result["extraction_status"] == "OK"
    assert "marker" in result["normalized_text"]


def test_text_is_bounded(tmp_path, cfg):
    cfg.section("documents")["max_text_characters"] = 20
    target = tmp_path / "big.txt"
    target.write_text("x" * 10_000, encoding="utf-8")
    result = extract_document(target, ".txt", cfg)
    assert result["character_count"] <= 20
