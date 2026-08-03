"""CI smoke test: an installed wheel must select and exercise its bundled Rust core."""

from __future__ import annotations

import hashlib
import tempfile
from pathlib import Path

from housekeeper.acceleration.capability_detection import detect_backend


def main() -> None:
    backend = detect_backend()
    try:
        capabilities = backend.capabilities()
        if capabilities.get("backend") != "rust":
            raise SystemExit(f"wheel did not select bundled Rust core: {capabilities}")
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "payload.bin"
            payload = b"housekeeper wheel rust smoke test"
            target.write_bytes(payload)
            result = backend.identity_hash(str(target), "sha256", 4096, 1024, 2)
            expected = hashlib.sha256(payload).hexdigest()
            if result.get("full_hash") != expected or result.get("quick_hash") != expected:
                raise SystemExit(f"bundled Rust digest mismatch: {result}")
    finally:
        close = getattr(backend, "close", None)
        if callable(close):
            close()


if __name__ == "__main__":
    main()
