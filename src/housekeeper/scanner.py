import hashlib
import os
import platform
import sys
import time
from pathlib import Path

from .config import AppConfig, config_fingerprint
from .constants import EntryType
from .database import Database
from .models import FileStatRecord
from .path_utils import (is_hidden_path, normalize_absolute_path,
                         safe_relative_path)


def build_source_root_fingerprint(source_root: Path) -> str:
    st = source_root.stat()
    return hashlib.sha256(
        f"{normalize_absolute_path(source_root)}|{st.st_dev}|{st.st_ino}".encode()
    ).hexdigest()


class DriveScanner:
    def __init__(self, database: Database, config: AppConfig):
        self.db, self.config = database, config

    def inspect_entry(self, path: Path, root: Path) -> FileStatRecord:
        try:
            st = path.stat(follow_symlinks=False)
            islink = path.is_symlink()
            kind = (
                EntryType.SYMLINK
                if islink
                else (
                    EntryType.DIRECTORY
                    if path.is_dir()
                    else EntryType.FILE if path.is_file() else EntryType.OTHER
                )
            )
            return FileStatRecord(
                path,
                safe_relative_path(path, root),
                path.name,
                kind,
                st.st_size,
                is_hidden_path(path),
                islink,
                os.readlink(path) if islink else None,
                st.st_mtime,
                st.st_ctime,
                st.st_mode,
                st.st_dev,
                getattr(st, "st_ino", None),
            )
        except OSError as exc:
            return FileStatRecord(
                path,
                safe_relative_path(path, root),
                path.name,
                EntryType.OTHER,
                read_error=str(exc),
            )

    def should_exclude(self, path: Path, root: Path) -> bool:
        rel = str(safe_relative_path(path, root))
        s = self.config.section("scanner")
        return rel in s.get("excluded_paths", []) or path.name in s.get(
            "excluded_names", []
        )

    def scan(self, source_root: Path, resume: bool = True):
        root = normalize_absolute_path(source_root)
        root_fp = build_source_root_fingerprint(root)
        self.db.initialize()
        old = (
            self.db.fetch_one(
                "SELECT id,status,source_root_fingerprint FROM scan_runs ORDER BY id DESC LIMIT 1"
            )
            if resume
            else None
        )
        run_id = (
            int(old["id"])
            if old
            and old["status"] != "COMPLETE"
            and old["source_root_fingerprint"] == root_fp
            else self.db.create_scan_run(
                str(root),
                root_fp,
                config_fingerprint(self.config),
                hostname=platform.node(),
                platform=platform.platform(),
                python_version=sys.version,
            )
        )
        counts = {"files": 0, "dirs": 0, "symlinks": 0, "errors": 0, "bytes": 0}
        stack = [(root, None)]
        while stack:
            directory, parent_id = stack.pop()
            try:
                entries = sorted(os.scandir(directory), key=lambda e: e.name.casefold())
            except OSError as exc:
                counts["errors"] += 1
                continue
            for dent in entries:
                path = Path(dent.path)
                if self.should_exclude(path, root):
                    continue
                rec = self.inspect_entry(path, root)
                values = {
                    "scan_run_id": run_id,
                    "parent_entry_id": parent_id,
                    "source_root": str(root),
                    "absolute_path": str(rec.path),
                    "relative_path": str(rec.relative_path),
                    "name": rec.name,
                    "suffix": path.suffix.lower(),
                    "entry_type": rec.entry_type,
                    "is_hidden": int(rec.is_hidden),
                    "is_symlink": int(rec.is_symlink),
                    "symlink_target": rec.symlink_target,
                    "size_bytes": rec.size_bytes,
                    "device_id": rec.device_id,
                    "inode_or_file_id": rec.inode_or_file_id,
                    "mode": rec.mode,
                    "created_at": rec.created_at,
                    "modified_at": rec.modified_at,
                    "scan_status": "ERROR" if rec.read_error else "OK",
                    "read_error": rec.read_error,
                }
                entry_id = self.db.insert_entry(values)
                if rec.entry_type == EntryType.FILE:
                    counts["files"] += 1
                    counts["bytes"] += rec.size_bytes
                elif rec.entry_type == EntryType.DIRECTORY:
                    counts["dirs"] += 1
                    stack.append((path, entry_id))
                elif rec.entry_type == EntryType.SYMLINK:
                    counts["symlinks"] += 1
                if rec.read_error:
                    counts["errors"] += 1
            self.db.connect().execute(
                "UPDATE scan_runs SET files_seen=?,directories_seen=?,symlinks_seen=?,errors_seen=?,bytes_seen=?,last_checkpoint_at=CURRENT_TIMESTAMP WHERE id=?",
                (*counts.values(), run_id),
            )
            self.db.connect().commit()
        self.db.execute(
            "UPDATE scan_runs SET status='COMPLETE',completed_at=CURRENT_TIMESTAMP WHERE id=?",
            (run_id,),
        )
        self.db.connect().commit()
        return counts
