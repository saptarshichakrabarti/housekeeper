from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class HashResult:
    digest: str | None
    size: int
    stable: bool
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class FileStatRecord:
    path: Path
    relative_path: Path
    name: str
    entry_type: str
    size_bytes: int = 0
    is_hidden: bool = False
    is_symlink: bool = False
    symlink_target: str | None = None
    modified_at: float | None = None
    created_at: float | None = None
    mode: int | None = None
    device_id: int | None = None
    inode_or_file_id: int | None = None
    read_error: str | None = None


@dataclass(frozen=True)
class ManifestEntry:
    approved: bool
    entry_id: int
    source_path: str
    relative_path: str
    size_bytes: int
    expected_sha256: str
    classification: str
    confidence: float
    reason_codes: list[str]
    explanation: str
    canonical_surviving_path: str | None = None
    reviewer_notes: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
