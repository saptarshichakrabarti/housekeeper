from pathlib import Path

from housekeeper.acceleration.capability_detection import detect_backend
from housekeeper.acceleration.python_backend import PythonBackend


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
