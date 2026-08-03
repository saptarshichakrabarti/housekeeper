import os
import shutil
import sys
from pathlib import Path

from .python_backend import PythonBackend
from .subprocess_backend import SubprocessBackend


def _installed_core() -> str | None:
    """The wheel-installed executable beside this environment's Python, even off ``PATH``."""
    name = "housekeeper-core.exe" if os.name == "nt" else "housekeeper-core"
    candidate = Path(sys.executable).parent / name
    return str(candidate) if candidate.is_file() else None


def detect_backend():
    override = os.environ.get("HOUSEKEEPER_CORE")
    # An explicit override is authoritative. Otherwise prefer the executable bundled into the
    # active Python environment over an unrelated same-named program found on the shell PATH.
    candidates = [override] if override else [_installed_core(), shutil.which("housekeeper-core")]
    for executable in dict.fromkeys(candidate for candidate in candidates if candidate):
        try:
            backend = SubprocessBackend(executable)
            if backend.capabilities().get("protocol_version") == "1":
                return backend
        except (OSError, RuntimeError, ValueError):
            continue
    return PythonBackend()
