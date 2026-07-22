import sys
from pathlib import Path

import pytest

from housekeeper.acceleration.capability_detection import detect_backend
from housekeeper.acceleration.python_backend import PythonBackend
from housekeeper.acceleration.subprocess_backend import SubprocessBackend

_SERVER = [sys.executable, "-m", "housekeeper.acceleration.server"]


def test_python_acceleration_contract(tmp_path):
    path = tmp_path / "data.bin"
    path.write_bytes(b"hello")
    result = PythonBackend().full_hash(str(path), "sha256", 4096)
    assert result["status"] == "ok"
    assert result["size_bytes"] == 5
    assert result["full_hash"] == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824"
    quick = PythonBackend().quick_hash(str(path), "sha256", 4, 1)
    assert quick["status"] == "ok"
    assert quick["quick_hash"]


def test_capability_detection_falls_back_safely(monkeypatch):
    monkeypatch.setenv("HOUSEKEEPER_CORE", str(Path("/does/not/exist")))
    assert detect_backend().capabilities()["backend"] == "python"


def test_subprocess_backend_matches_python_backend(tmp_path):
    """Equivalence contract: the JSONL subprocess backend must match the in-process backend."""
    path = tmp_path / "data.bin"
    path.write_bytes(b"contract-payload" * 5000)
    reference = PythonBackend().full_hash(str(path))
    remote = SubprocessBackend(_SERVER).full_hash(str(path))
    assert remote["status"] == "ok"
    assert remote["full_hash"] == reference["full_hash"]
    assert remote["size_bytes"] == reference["size_bytes"]
    quick_ref = PythonBackend().quick_hash(str(path), "sha256", 1024, 2)
    quick_remote = SubprocessBackend(_SERVER).quick_hash(str(path), "sha256", 1024, 2)
    assert quick_remote["quick_hash"] == quick_ref["quick_hash"]


def test_subprocess_capabilities_and_errors():
    caps = SubprocessBackend(_SERVER).capabilities()
    assert caps["backend"] == "python"
    assert "full_hash" in caps["operations"]
    # An unsupported operation is reported as a structured error, never a crash.
    with pytest.raises(RuntimeError):
        SubprocessBackend(_SERVER).request("not_a_real_operation", {})


def test_manifest_verification_equivalence(tmp_path):
    import hashlib

    path = tmp_path / "f.bin"
    payload = b"verify-me" * 100
    path.write_bytes(payload)
    entry = {
        "path": str(path),
        "expected_hash": hashlib.sha256(payload).hexdigest(),
        "expected_size": len(payload),
    }
    reference = PythonBackend().verify_manifest([entry])
    remote = SubprocessBackend(_SERVER).request("verify_manifest", {"entries": [entry]})
    assert reference["valid"] is True
    assert remote["valid"] is True
