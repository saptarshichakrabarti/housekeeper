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


def test_the_backend_process_is_reused_across_requests(tmp_path):
    """The backend is a request loop; one process per request threw that away.

    Measured at 14.7 ms per hash forked against 0.57 ms persistent — a 26x *deceleration* over
    doing the work in-process, on exactly the small files an inventory is mostly made of.
    """
    path = tmp_path / "data.bin"
    path.write_bytes(b"reuse-me" * 100)
    backend = SubprocessBackend(_SERVER)
    try:
        backend.full_hash(str(path))
        first = backend._process
        assert first is not None and first.poll() is None
        for _ in range(5):
            backend.full_hash(str(path))
        assert backend._process is first, "a new process per request is the bug being fixed"
    finally:
        backend.close()
    assert first.poll() is not None, "close() must not leak the backend process"


def test_a_wedged_backend_times_out_rather_than_hanging(tmp_path):
    backend = SubprocessBackend([sys.executable, "-c", "import time; time.sleep(30)"], timeout=1.0)
    try:
        with pytest.raises(RuntimeError, match="timed out|exited"):
            backend.capabilities()
        assert backend._process is None, "a timed-out backend must be discarded, not reused"
    finally:
        backend.close()


@pytest.mark.parametrize("size", [0, 1, 100, 4095, 4096, 12288, 16384, 16385, 40960, 100_003])
def test_native_backend_is_byte_identical_to_python(tmp_path, size):
    """The equivalence claim, at every size where the two implementations could disagree.

    4096 is the sample chunk used here and 16384 = (samples + 2) * chunk is the threshold below
    which Python reads the whole file instead of sampling it. The native backend did not implement
    that rule, did not sort or deduplicate its offsets, and read once per offset where Python loops
    on a short read — it disagreed with Python on every size tested.
    """
    binary = Path(__file__).resolve().parents[1] / "rust" / "target" / "release" / "housekeeper-core"
    if not binary.is_file():
        pytest.skip("native backend not built (make rust)")
    path = tmp_path / "data.bin"
    path.write_bytes(bytes((index * 31 + 7) % 251 for index in range(size)))
    def payload(reply, digest_field):
        return {key: reply.get(key) for key in ("status", digest_field, "size_bytes", "stable")}

    native = SubprocessBackend([str(binary)])
    try:
        assert native.capabilities()["backend"] == "rust"
        assert payload(native.full_hash(str(path), "sha256", 4096), "full_hash") == payload(
            PythonBackend().full_hash(str(path), "sha256", 4096), "full_hash"
        )
        assert payload(native.quick_hash(str(path), "sha256", 4096, 2), "quick_hash") == payload(
            PythonBackend().quick_hash(str(path), "sha256", 4096, 2), "quick_hash"
        )
    finally:
        native.close()


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
