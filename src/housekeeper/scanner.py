import hashlib
import json
import os
import platform
import sys
from pathlib import Path

from .config import AppConfig, config_fingerprint, performance_profile
from .constants import EntryType
from .database import Database
from .models import FileStatRecord
from .path_utils import is_hidden_path, normalize_absolute_path, safe_relative_path
from .jobs import JobCancelled, JobPaused, check_cancelled, create_job, update_job
from .core.scan_identity import discover_source_identity
from .hashing import compute_quick_hash


def build_source_root_fingerprint(source_root: Path) -> str:
    st = source_root.stat()
    filesystem_uuid, _label, metadata = discover_source_identity(source_root)
    return hashlib.sha256(
        f"{filesystem_uuid or ''}|{st.st_dev}|{st.st_ino}|{getattr(st, 'st_rdev', 0)}|{json.dumps(metadata, sort_keys=True)}".encode()
    ).hexdigest()


def stat_fingerprint(record: FileStatRecord) -> str:
    """A reuse hint, never a substitute for a verified content hash."""
    return hashlib.sha256(
        f"{record.entry_type}|{record.size_bytes}|{record.modified_at}|{record.device_id}|{record.inode_or_file_id}".encode()
    ).hexdigest()


class DriveScanner:
    def __init__(self, database: Database, config: AppConfig):
        self.db, self.config = database, config
        # The scan_run id written by the most recent scan() call (set before scan returns), so a
        # caller can scope follow-up analysis to exactly the run just produced rather than guessing
        # via MAX(scan_runs.id) — which is wrong when a scan resumes an earlier interrupted run.
        self.last_run_id: int | None = None

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
                    else EntryType.FILE
                    if path.is_file()
                    else EntryType.OTHER
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
        return rel in s.get("excluded_paths", []) or path.name in s.get("excluded_names", [])

    def scan(
        self,
        source_root: Path,
        resume: bool = True,
        incremental: bool = True,
        changed_only: bool = False,
        force_rehash: bool = False,
        job_id: int | None = None,
    ):
        root = normalize_absolute_path(source_root)
        profile = performance_profile(self.config, root)
        root_fp = build_source_root_fingerprint(root)
        filesystem_uuid, volume_label, device_metadata = discover_source_identity(root)
        self.db.initialize()
        # A user may explicitly associate a remounted path when the filesystem exposes no
        # portable UUID.  Preserve that selected source identity instead of making a twin.
        associated = self.db.fetch_one(
            "SELECT source_fingerprint FROM source_roots WHERE last_mount_path=?", (str(root),)
        )
        if associated:
            root_fp = str(associated["source_fingerprint"])
        old = (
            self.db.fetch_one(
                "SELECT id,status,source_root_fingerprint FROM scan_runs WHERE source_root_fingerprint=? ORDER BY id DESC LIMIT 1",
                (root_fp,),
            )
            if resume
            else None
        )
        run_id = (
            int(old["id"])
            if old and old["status"] != "COMPLETE"
            else self.db.create_scan_run(
                str(root),
                root_fp,
                config_fingerprint(self.config),
                hostname=platform.node(),
                platform=platform.platform(),
                python_version=sys.version,
            )
        )
        self.last_run_id = run_id
        previous = (
            self.db.fetch_one(
                "SELECT id FROM scan_runs WHERE source_root_fingerprint=? AND id<>? AND status='COMPLETE' ORDER BY id DESC LIMIT 1",
                (root_fp, run_id),
            )
            if incremental
            else None
        )
        self.db.connect().execute(
            "INSERT INTO source_roots(display_name,source_fingerprint,filesystem_uuid,volume_label,device_metadata_json,last_mount_path) VALUES(?,?,?,?,?,?) ON CONFLICT(source_fingerprint) DO UPDATE SET last_seen_at=CURRENT_TIMESTAMP,last_mount_path=excluded.last_mount_path,filesystem_uuid=COALESCE(excluded.filesystem_uuid,source_roots.filesystem_uuid),volume_label=COALESCE(excluded.volume_label,source_roots.volume_label),device_metadata_json=excluded.device_metadata_json",
            (
                root.name or str(root),
                root_fp,
                filesystem_uuid,
                volume_label,
                json.dumps(device_metadata, sort_keys=True),
                str(root),
            ),
        )
        self.db.connect().commit()
        source_root_row = self.db.fetch_one(
            "SELECT id FROM source_roots WHERE source_fingerprint=?", (root_fp,)
        )
        assert source_root_row is not None
        source_root_id = int(source_root_row["id"])
        self.db.connect().execute(
            "UPDATE source_roots SET device_metadata_json=json_set(COALESCE(device_metadata_json,'{}'),'$.storage_profile',?) WHERE id=?",
            (str(profile["profile_name"]), source_root_id),
        )
        self.db.connect().commit()
        job_id = job_id or create_job(
            self.db,
            "SCAN",
            {
                "source_root": str(root),
                "incremental": incremental,
                "storage_profile": profile["profile_name"],
            },
            config_fingerprint(self.config),
            worker_count=int(profile["scan_workers"]),
        )
        update_job(self.db, job_id, "RUNNING")
        counts = {"files": 0, "dirs": 0, "symlinks": 0, "errors": 0, "bytes": 0}
        processed = 0
        stack: list[tuple[Path, int | None]] = [(root, None)]
        try:
            while stack:
                check_cancelled(self.db, job_id)
                directory, parent_id = stack.pop()
                try:
                    entries = sorted(os.scandir(directory), key=lambda e: e.name.casefold())
                except OSError as exc:
                    counts["errors"] += 1
                    update_job(
                        self.db,
                        job_id,
                        "RUNNING",
                        error_count=counts["errors"],
                        current_item=f"{directory}: {exc}",
                    )
                    continue
                for dent in entries:
                    check_cancelled(self.db, job_id)
                    path = Path(dent.path)
                    if self.should_exclude(path, root):
                        continue
                    rec = self.inspect_entry(path, root)
                    entry_id = self.db.insert_entry(
                        {
                            "scan_run_id": run_id,
                            "parent_entry_id": parent_id,
                            "source_root_id": source_root_id,
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
                    )
                    processed += 1
                    fingerprint = stat_fingerprint(rec)
                    unchanged = False
                    prior = None
                    rename_evidence: dict[str, object] | None = None
                    if previous and not force_rehash:
                        prior = self.db.fetch_one(
                            "SELECT e.id,e.size_bytes,e.modified_at,e.device_id,e.inode_or_file_id,s.quick_hash,s.full_hash,s.hash_algorithm,s.hash_status,s.hash_error,s.full_hash_computed_at,l.content_object_id,l.link_status FROM filesystem_entries e LEFT JOIN file_signatures s ON s.entry_id=e.id LEFT JOIN entry_content_links l ON l.entry_id=e.id WHERE e.scan_run_id=? AND e.relative_path=?",
                            (previous["id"], str(rec.relative_path)),
                        )
                        if prior:
                            unchanged = (
                                prior["size_bytes"],
                                prior["modified_at"],
                                prior["device_id"],
                                prior["inode_or_file_id"],
                            ) == (
                                rec.size_bytes,
                                rec.modified_at,
                                rec.device_id,
                                rec.inode_or_file_id,
                            )
                        if (
                            unchanged
                            and prior
                            and prior["full_hash"]
                            and rec.entry_type == EntryType.FILE
                        ):
                            self.db.connect().execute(
                                "INSERT OR REPLACE INTO file_signatures(entry_id,quick_hash,full_hash,hash_algorithm,hash_status,hash_error,full_hash_computed_at) VALUES(?,?,?,?,?,?,?)",
                                (
                                    entry_id,
                                    prior["quick_hash"],
                                    prior["full_hash"],
                                    prior["hash_algorithm"],
                                    prior["hash_status"],
                                    prior["hash_error"],
                                    prior["full_hash_computed_at"],
                                ),
                            )
                            if prior["content_object_id"]:
                                self.db.link_entry_content(
                                    entry_id,
                                    prior["content_object_id"],
                                    fingerprint,
                                    prior["link_status"] or "VERIFIED",
                                )
                        elif rec.entry_type == EntryType.FILE:
                            # Cross-directory moves generally lose inode identity on copied
                            # backups. A matching quick hash is evidence only; it never grants
                            # content reuse until a full hash/link is verified later.
                            quick_candidates = self.db.iter_rows(
                                """SELECT e.id,e.relative_path,s.quick_hash FROM filesystem_entries e
                                   JOIN file_signatures s ON s.entry_id=e.id
                                   WHERE e.scan_run_id=? AND e.size_bytes=? AND s.quick_hash IS NOT NULL""",
                                (previous["id"], rec.size_bytes),
                            )
                            quick = None
                            for candidate in quick_candidates:
                                if quick is None:
                                    quick = compute_quick_hash(
                                        path,
                                        self.config.section("hashing")["quick_hash_chunk_bytes"],
                                        self.config.section("hashing")["quick_hash_middle_samples"],
                                        self.config.section("hashing")["algorithm"],
                                    )
                                if quick.stable and quick.digest == candidate["quick_hash"]:
                                    rename_evidence = {
                                        "previous_entry_id": candidate["id"],
                                        "previous_relative_path": candidate["relative_path"],
                                        "quick_hash_match": True,
                                        "requires_full_hash_confirmation": True,
                                    }
                                    self.db.connect().execute(
                                        "INSERT OR REPLACE INTO file_signatures(entry_id,quick_hash,hash_algorithm,hash_status) VALUES(?,?,?,'QUICK_MATCH')",
                                        (
                                            entry_id,
                                            quick.digest,
                                            self.config.section("hashing")["algorithm"],
                                        ),
                                    )
                                    break
                        if prior is None and rec.entry_type == EntryType.FILE:
                            # A path can return after an intermittent mount or an earlier
                            # absence. Keep explicit historical evidence, but require a full
                            # hash before linking it to any prior content object.
                            reappeared = self.db.fetch_one(
                                """SELECT old.id,old.scan_run_id FROM filesystem_entries old
                                   JOIN scan_entry_changes change ON change.entry_id=old.id
                                   WHERE old.source_root_id=? AND old.relative_path=?
                                   AND change.change_status='MISSING'
                                   ORDER BY change.id DESC LIMIT 1""",
                                (source_root_id, str(rec.relative_path)),
                            )
                            if reappeared:
                                rename_evidence = {
                                    "reappeared_after_missing": True,
                                    "previous_entry_id": reappeared["id"],
                                    "missing_scan_run_id": reappeared["scan_run_id"],
                                    "requires_full_hash_confirmation": True,
                                }
                        change_status = (
                            "ERROR"
                            if rec.read_error
                            else "UNCHANGED"
                            if unchanged
                            else "CONTENT_POSSIBLY_CHANGED"
                            if prior and rec.entry_type == EntryType.FILE
                            else "METADATA_CHANGED"
                            if prior
                            else "NEW"
                        )
                        if rename_evidence:
                            change_status = "MOVED_OR_RENAMED_CANDIDATE"
                        self.db.connect().execute(
                            "INSERT INTO scan_entry_changes(scan_run_id,entry_id,relative_path,change_status,evidence_json) VALUES(?,?,?,?,?)",
                            (
                                run_id,
                                entry_id,
                                str(rec.relative_path),
                                change_status,
                                json.dumps(
                                    {
                                        "stat_fingerprint": fingerprint,
                                        "prior_entry_id": prior["id"] if prior else None,
                                        "read_error": rec.read_error,
                                    },
                                    sort_keys=True,
                                ),
                            ),
                        )
                        if rename_evidence:
                            self.db.connect().execute(
                                "UPDATE scan_entry_changes SET evidence_json=? WHERE scan_run_id=? AND entry_id=?",
                                (
                                    json.dumps(
                                        {"stat_fingerprint": fingerprint, **rename_evidence},
                                        sort_keys=True,
                                    ),
                                    run_id,
                                    entry_id,
                                ),
                            )
                        if prior and not unchanged:
                            self.db.connect().execute(
                                "UPDATE review_decisions SET stale=1,updated_at=CURRENT_TIMESTAMP WHERE target_type='ENTRY' AND target_id=? AND current=1",
                                (prior["id"],),
                            )
                    if changed_only and unchanged:
                        continue
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
                update_job(
                    self.db,
                    job_id,
                    "RUNNING",
                    processed_count=processed,
                    success_count=processed - counts["errors"],
                    error_count=counts["errors"],
                    current_item=str(directory),
                    checkpoint={
                        "directory": str(directory),
                        "pending_directories": len(stack),
                        "scan_run_id": run_id,
                    },
                )
        except (JobCancelled, JobPaused):
            # A scan that stops early — cancelled outright or paused at a checkpoint — leaves an
            # incomplete inventory; mark the run INTERRUPTED so it is never mistaken for a full scan.
            # (check_cancelled has already settled the job row itself to CANCELLED/PAUSED.)
            self.db.execute("UPDATE scan_runs SET status='INTERRUPTED' WHERE id=?", (run_id,))
            self.db.connect().commit()
            raise
        if previous and incremental:
            # Set-based diffing keeps the memory footprint bounded on million-entry scans.
            self.db.connect().execute(
                """INSERT INTO scan_entry_changes(scan_run_id,entry_id,relative_path,change_status,evidence_json)
                SELECT ?,old.id,old.relative_path,'MISSING',json_object('previous_size',old.size_bytes)
                FROM filesystem_entries old LEFT JOIN filesystem_entries current
                ON current.scan_run_id=? AND current.relative_path=old.relative_path
                WHERE old.scan_run_id=? AND current.id IS NULL""",
                (run_id, run_id, previous["id"]),
            )
            self.db.connect().execute(
                """UPDATE review_decisions SET stale=1,updated_at=CURRENT_TIMESTAMP
                WHERE target_type='ENTRY' AND current=1 AND target_id IN
                (SELECT entry_id FROM scan_entry_changes WHERE scan_run_id=? AND change_status='MISSING')""",
                (run_id,),
            )
            self.db.connect().execute(
                """UPDATE scan_entry_changes AS fresh SET change_status='MOVED_OR_RENAMED_CANDIDATE',
                evidence_json=json_object('previous_entry_id',old.id,'previous_relative_path',old.relative_path,
                'same_device_inode',1,'same_size',1)
                FROM filesystem_entries current JOIN filesystem_entries old
                ON old.scan_run_id=? AND old.device_id=current.device_id AND old.inode_or_file_id=current.inode_or_file_id
                AND old.size_bytes=current.size_bytes
                WHERE fresh.scan_run_id=? AND fresh.entry_id=current.id AND fresh.change_status='NEW'
                AND NOT EXISTS(SELECT 1 FROM filesystem_entries existing WHERE existing.scan_run_id=? AND existing.relative_path=old.relative_path)""",
                (previous["id"], run_id, run_id),
            )
            self.db.connect().execute(
                """UPDATE scan_entry_changes AS fresh SET change_status='MOVED_OR_RENAMED_CANDIDATE',
                evidence_json=json_object('previous_entry_id',(
                    SELECT old.entry_id FROM scan_entry_changes old JOIN entry_content_links old_link ON old_link.entry_id=old.entry_id
                    JOIN entry_content_links fresh_link ON fresh_link.entry_id=fresh.entry_id AND fresh_link.content_object_id=old_link.content_object_id
                    WHERE old.scan_run_id=fresh.scan_run_id AND old.change_status='MISSING' AND old_link.link_status='VERIFIED' AND fresh_link.link_status='VERIFIED' LIMIT 1),
                    'verified_content_match',1)
                WHERE fresh.scan_run_id=? AND fresh.change_status='NEW' AND EXISTS(
                    SELECT 1 FROM scan_entry_changes old JOIN entry_content_links old_link ON old_link.entry_id=old.entry_id
                    JOIN entry_content_links fresh_link ON fresh_link.entry_id=fresh.entry_id AND fresh_link.content_object_id=old_link.content_object_id
                    WHERE old.scan_run_id=fresh.scan_run_id AND old.change_status='MISSING' AND old_link.link_status='VERIFIED' AND fresh_link.link_status='VERIFIED')""",
                (run_id,),
            )
            self.db.connect().commit()
        self.db.execute(
            "UPDATE scan_runs SET status='COMPLETE',completed_at=CURRENT_TIMESTAMP WHERE id=?",
            (run_id,),
        )
        self.db.connect().commit()
        self.db.connect().execute("DELETE FROM graph_layout_cache")
        self.db.connect().commit()
        update_job(
            self.db,
            job_id,
            "COMPLETED",
            processed_count=sum(counts.values()),
            success_count=sum(counts.values()),
        )
        self.db.refresh_materialized_summaries(run_id)
        return counts
