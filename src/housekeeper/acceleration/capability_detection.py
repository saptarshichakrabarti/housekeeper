import os
import shutil

from .python_backend import PythonBackend
from .subprocess_backend import SubprocessBackend


def detect_backend():
    executable = os.environ.get("HOUSEKEEPER_CORE") or shutil.which("housekeeper-core")
    if executable:
        try:
            backend = SubprocessBackend(executable)
            if backend.capabilities().get("protocol_version") == "1":
                return backend
        except (OSError, RuntimeError, ValueError):
            pass
    return PythonBackend()
