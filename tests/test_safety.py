from pathlib import Path

from housekeeper.analyzers.document_versions import normalize_version_filename
from housekeeper.hashing import compute_full_hash
from housekeeper.path_utils import safe_destination_path


def test_destination_cannot_escape(tmp_path):
    try:
        safe_destination_path(tmp_path, Path("../outside"))
        assert False
    except ValueError:
        pass


def test_hash_is_streamed_and_stable(tmp_path):
    p = tmp_path / "x"
    p.write_bytes(b"abc")
    h = compute_full_hash(p, "sha256", 2)
    assert h.stable and h.size == 3


def test_version_stem_preserves_family():
    assert normalize_version_filename("Report final v2.docx") == "report.docx"
