"""Reference JSONL acceleration server (the Python backend speaking the subprocess protocol).

This is the fallback / contract reference: a future Rust ``housekeeper-core`` binary must produce
byte-identical results for the same requests. Run with ``python -m housekeeper.acceleration.server``.
"""

from __future__ import annotations

import json
import sys
from typing import Any, TextIO

from .python_backend import PythonBackend


def _handle(backend: PythonBackend, request: dict[str, Any]) -> dict[str, Any]:
    operation = request.get("operation")
    arguments = request.get("arguments", {})
    if operation == "capabilities":
        return {"status": "ok", "capabilities": backend.capabilities()}
    if operation == "full_hash":
        return backend.full_hash(
            arguments["path"], arguments.get("algorithm", "sha256"), arguments.get("block_size", 8_388_608)
        )
    if operation == "quick_hash":
        return backend.quick_hash(
            arguments["path"],
            arguments.get("algorithm", "sha256"),
            arguments.get("chunk_size", 1_048_576),
            arguments.get("middle_samples", 2),
        )
    if operation == "verify_manifest":
        return backend.verify_manifest(arguments.get("entries", []))
    if operation == "aggregate_directories":
        return backend.aggregate_directories(arguments.get("entries", []))
    return {"status": "error", "error": f"unsupported operation: {operation}"}


def serve(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> None:
    backend = PythonBackend()
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        request = json.loads(line)
        request_id = request.get("request_id")
        try:
            result = _handle(backend, request)
        except Exception as exc:  # noqa: BLE001 - report as a structured protocol error
            result = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
        result.update({"request_id": request_id, "event": "result"})
        stdout.write(json.dumps(result) + "\n")
        stdout.flush()


if __name__ == "__main__":
    serve()
