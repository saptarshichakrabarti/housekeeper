"""JSONL client for a persistent out-of-process acceleration backend.

One long-lived process (not ``subprocess.run`` per hash — spawn cost dominated small-file work).
On timeout, kill the process: a late reply would otherwise corrupt the next request on the pipe.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from typing import Any, Self


class SubprocessBackend:
    protocol_version = "1"

    def __init__(self, executable: str | list[str], timeout: float = 300):
        self.command = [executable] if isinstance(executable, str) else list(executable)
        self.executable = self.command[0]
        self.timeout = timeout
        self._process: subprocess.Popen | None = None
        self._reader: ThreadPoolExecutor | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _ensure_process(self) -> subprocess.Popen:
        if self._process is None or self._process.poll() is not None:
            if self._reader is not None:
                self._reader.shutdown(wait=False, cancel_futures=True)
            self._process = subprocess.Popen(
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            # One reader thread, so a wedged backend is a timeout rather than a hang. Created with
            # the process so it is discarded with it.
            self._reader = ThreadPoolExecutor(max_workers=1)
        return self._process

    def close(self) -> None:
        process, self._process = self._process, None
        reader, self._reader = self._reader, None
        if process is not None and process.poll() is None:
            try:
                if process.stdin is not None:
                    process.stdin.close()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                process.kill()
                process.wait()
        if reader is not None:
            reader.shutdown(wait=False, cancel_futures=True)

    def _kill(self) -> None:
        """Discard a backend mid-conversation. Never reuse one whose reply is still in flight."""
        process, self._process = self._process, None
        reader, self._reader = self._reader, None
        if process is not None:
            process.kill()
            process.wait()
        if reader is not None:
            reader.shutdown(wait=False, cancel_futures=True)

    def _request(self, operation: str, arguments: dict) -> dict[str, Any]:
        request_id = str(uuid.uuid4())
        payload = json.dumps(
            {
                "protocol_version": self.protocol_version,
                "request_id": request_id,
                "operation": operation,
                "arguments": arguments,
            }
        )
        process = self._ensure_process()
        try:
            assert process.stdin is not None
            process.stdin.write(payload + "\n")
            process.stdin.flush()
        except (BrokenPipeError, OSError) as exc:
            self._kill()
            raise RuntimeError(f"acceleration backend closed its input: {exc}") from exc

        reader, stdout = self._reader, process.stdout
        assert reader is not None and stdout is not None
        while True:
            try:
                line = reader.submit(stdout.readline).result(timeout=self.timeout)
            except FutureTimeout:
                self._kill()
                raise RuntimeError(
                    f"acceleration backend timed out after {self.timeout}s on {operation}"
                ) from None
            if not line:
                stderr = process.stderr.read() if process.stderr else ""
                code = process.poll()
                self._kill()
                raise RuntimeError(f"acceleration backend exited {code}: {stderr.strip()}")
            if not line.strip():
                continue
            response = json.loads(line)
            if response.get("event") != "result":
                continue
            if response.get("request_id") not in {None, request_id}:
                # Out of step with the protocol — the safe move is a fresh process, not a guess.
                self._kill()
                raise RuntimeError("acceleration backend replied out of order")
            if response.get("status") != "ok":
                raise RuntimeError(response.get("error", "acceleration operation failed"))
            return response

    def capabilities(self):
        return self._request("capabilities", {}).get("capabilities", {})

    def full_hash(self, path: str, algorithm: str = "sha256", block_size: int = 8_388_608):
        return self._request(
            "full_hash", {"path": path, "algorithm": algorithm, "block_size": block_size}
        )

    def quick_hash(
        self,
        path: str,
        algorithm: str = "sha256",
        chunk_size: int = 1_048_576,
        middle_samples: int = 2,
    ):
        return self._request(
            "quick_hash",
            {
                "path": path,
                "algorithm": algorithm,
                "chunk_size": chunk_size,
                "middle_samples": middle_samples,
            },
        )

    def identity_hash(
        self,
        path: str,
        algorithm: str = "blake3",
        block_size: int = 8_388_608,
        quick_chunk_size: int = 1_048_576,
        middle_samples: int = 2,
    ):
        return self._request(
            "identity_hash",
            {
                "path": path,
                "algorithm": algorithm,
                "block_size": block_size,
                "quick_chunk_size": quick_chunk_size,
                "middle_samples": middle_samples,
            },
        )

    def request(self, operation: str, arguments: dict):
        return self._request(operation, arguments)
