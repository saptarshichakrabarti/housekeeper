"""File-type detection tests: signatures win over extensions; high-level buckets."""

from housekeeper.filetypes import (
    classify_high_level_type,
    detect_file_type,
    is_supported_archive,
    is_supported_document,
    is_supported_image,
)


def test_png_detected_by_signature_over_extension(tmp_path):
    target = tmp_path / "mislabeled.txt"
    target.write_bytes(b"\x89PNG\r\n\x1a\n rest")
    signature = detect_file_type(target)
    assert signature.detected_type == "png"
    assert is_supported_image(signature)
    assert classify_high_level_type(signature) == "image"


def test_pdf_detected_by_signature(tmp_path):
    target = tmp_path / "doc.pdf"
    target.write_bytes(b"%PDF-1.7\n...")
    signature = detect_file_type(target)
    assert signature.detected_type == "pdf"
    assert is_supported_document(signature)


def test_zip_container_detected(tmp_path):
    target = tmp_path / "a.zip"
    target.write_bytes(b"PK\x03\x04rest")
    signature = detect_file_type(target)
    assert is_supported_archive(signature)


def test_unknown_binary_is_other(tmp_path):
    target = tmp_path / "unknown.bin"
    target.write_bytes(bytes([0xDE, 0xAD, 0xBE, 0xEF]))
    signature = detect_file_type(target)
    assert classify_high_level_type(signature) == "other"
