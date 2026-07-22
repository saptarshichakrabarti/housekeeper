import json
import subprocess
import uuid


class SubprocessBackend:
    protocol_version = "1"

    def __init__(self, executable: "str | list[str]", timeout: float = 300):
        self.command = [executable] if isinstance(executable, str) else list(executable)
        self.executable = self.command[0]
        self.timeout = timeout

    def _request(self, operation: str, arguments: dict):
        request_id = str(uuid.uuid4())
        process = subprocess.run(
            self.command,
            input=json.dumps(
                {
                    "protocol_version": self.protocol_version,
                    "request_id": request_id,
                    "operation": operation,
                    "arguments": arguments,
                }
            )
            + "\n",
            text=True,
            capture_output=True,
            timeout=self.timeout,
            check=False,
        )
        if process.returncode != 0:
            raise RuntimeError(
                f"acceleration backend exited {process.returncode}: {process.stderr.strip()}"
            )
        for line in process.stdout.splitlines():
            response = json.loads(line)
            if (
                response.get("request_id") in {None, request_id}
                and response.get("event") == "result"
            ):
                if response.get("status") != "ok":
                    raise RuntimeError(response.get("error", "acceleration operation failed"))
                return response
        raise RuntimeError("acceleration backend returned no result")

    def capabilities(self):
        return self._request("capabilities", {}).get("capabilities", {})

    def full_hash(self, path: str, algorithm: str = "sha256", block_size: int = 8_388_608):
        return self._request(
            "full_hash", {"path": path, "algorithm": algorithm, "block_size": block_size}
        )

    def quick_hash(self, path: str, algorithm: str = "sha256", chunk_size: int = 1_048_576, middle_samples: int = 2):
        return self._request("quick_hash", {"path": path, "algorithm": algorithm, "chunk_size": chunk_size, "middle_samples": middle_samples})

    def request(self, operation: str, arguments: dict):
        return self._request(operation, arguments)
