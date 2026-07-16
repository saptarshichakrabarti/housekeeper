from enum import StrEnum


class EntryType(StrEnum):
    FILE = "file"
    DIRECTORY = "directory"
    SYMLINK = "symlink"
    OTHER = "other"


class Classification(StrEnum):
    KEEP = "KEEP"
    KEEP_CANONICAL = "KEEP_CANONICAL"
    REVIEW_SAFE = "REVIEW_SAFE"
    REVIEW_PROBABLE = "REVIEW_PROBABLE"
    REVIEW_VERSION_FAMILY = "REVIEW_VERSION_FAMILY"
    REVIEW_BACKUP = "REVIEW_BACKUP"
    REVIEW_LARGE = "REVIEW_LARGE"
    PROTECTED = "PROTECTED"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"


class JobStatus(StrEnum):
    RUNNING = "RUNNING"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"
    INTERRUPTED = "INTERRUPTED"


class MoveStatus(StrEnum):
    PLANNED = "PLANNED"
    MOVED = "MOVED"
    FAILED = "FAILED"
    PARTIAL = "PARTIAL"


SCHEMA_VERSION = 4
PROTECTED_SUFFIXES = {
    ".pem",
    ".key",
    ".kdbx",
    ".wallet",
    ".db",
    ".sqlite",
    ".sqlite3",
    ".gpg",
}
