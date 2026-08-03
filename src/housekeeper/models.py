from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from .constants import LEGACY_HASH_ALGORITHM


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
    #: Hard-link count from ``stat``. > 1 is the filesystem's own assertion that this path shares
    #: storage with another, which is what makes inode-based identity reuse safe to trust.
    nlink: int | None = None
    read_error: str | None = None


@dataclass(frozen=True)
class ManifestEntry:
    approved: bool
    entry_id: int
    source_path: str
    relative_path: str
    size_bytes: int
    expected_hash: str
    classification: str
    confidence: float
    reason_codes: list[str]
    explanation: str
    canonical_surviving_path: str | None = None
    reviewer_notes: str = ""
    #: Which function produced ``expected_hash``. Trailing with a default so a manifest written
    #: before this field existed — where the digest was always SHA-256 — still loads unchanged.
    expected_hash_algorithm: str = LEGACY_HASH_ALGORITHM

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
