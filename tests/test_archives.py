"""Archive-inspection tests: metadata only, malformed handling, traversal/limit guards."""

import io
import tarfile
import zipfile

import pytest

from housekeeper.analyzers.archives import (
    detect_archive_kind,
    inspect_archive,
    normalize_archive_member_path,
)
from housekeeper.config import load_config


@pytest.fixture
def cfg():
    return load_config()


def test_normalize_member_path():
    assert normalize_archive_member_path("a\\b\\c.txt") == "a/b/c.txt"
    assert normalize_archive_member_path("/leading/slash") == "leading/slash"


def test_detect_archive_kind(tmp_path):
    zip_path = tmp_path / "a.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        archive.writestr("x.txt", "y")
    assert detect_archive_kind(zip_path) == "zip"


def test_inspect_zip_metadata_only(tmp_path, cfg):
    path = tmp_path / "a.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("inside/report.txt", "body")
        archive.writestr("inside/data.bin", b"\x00\x01")
    result = inspect_archive(path, cfg)
    assert result["analysis_status"] == "OK"
    assert result["member_count"] == 2
    # Members are inspected, never extracted.
    assert result.get("nested_analysis") == "NOT_EXPANDED"


def test_inspect_targz(tmp_path, cfg):
    path = tmp_path / "a.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        payload = b"hello"
        info = tarfile.TarInfo("a.txt")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    result = inspect_archive(path, cfg)
    assert result["analysis_status"] == "OK"


def test_malformed_zip_is_error_not_disposable(tmp_path, cfg):
    path = tmp_path / "corrupt.zip"
    path.write_bytes(b"PK\x03\x04 truncated garbage")
    result = inspect_archive(path, cfg)
    assert result["analysis_status"] == "ERROR"


def test_path_traversal_member_is_error(tmp_path, cfg):
    path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("../escape.txt", "unsafe")
    result = inspect_archive(path, cfg)
    assert result["analysis_status"] == "ERROR"


def test_member_count_limit(tmp_path, cfg):
    path = tmp_path / "many.zip"
    with zipfile.ZipFile(path, "w") as archive:
        for i in range(5):
            archive.writestr(f"f{i}.txt", "x")
    cfg.section("archives")["max_members"] = 2
    result = inspect_archive(path, cfg)
    assert result["analysis_status"] == "ERROR"
