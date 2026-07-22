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


def _snapshot(root: Path) -> dict:
    """(relative path) -> (size, mtime) for every file under root, to prove nothing changed."""
    return {
        str(p.relative_to(root)): (p.stat().st_size, p.stat().st_mtime)
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def test_gui_driven_quickstart_leaves_source_tree_byte_identical(tmp_path):
    """The operational GUI's Scan button runs the same read-only pipeline as the CLI: it must
    never move, delete, or modify anything under the source tree it scans."""
    import time

    from housekeeper.config import load_config
    from housekeeper.dashboard.runner import OperationRunner

    source = tmp_path / "drive"
    source.mkdir()
    (source / "a.txt").write_text("hello", encoding="utf-8")
    (source / "b.txt").write_text("hello", encoding="utf-8")  # an exact duplicate
    before = _snapshot(source)

    config = load_config(workspace_override=tmp_path / "workspace")
    runner = OperationRunner(config)
    assert runner.submit("quickstart", source=str(source)) == "accepted"
    deadline = time.monotonic() + 15
    while runner.status()["state"] == "running":
        assert time.monotonic() < deadline, "operation did not finish in time"
        time.sleep(0.02)
    assert runner.status()["state"] == "idle"

    assert _snapshot(source) == before
