"""Filesystem traversal and incremental diffing.

The unit of work is a **batch**, not an entry. Traversal stages rows into ``filesystem_entries`` a
few thousand at a time, and everything that used to be decided per entry with its own queries —
what changed, which signatures and content links can be reused, what went missing, what looks
renamed — is decided afterwards with a handful of set-based statements. The correct idiom was
already here: the missing-entry epilogue at the bottom of the old loop worked exactly this way.
"""

import hashlib
import json
import os
import platform
import sys
from pathlib import Path

from .config import AppConfig, config_fingerprint, performance_profile
from .constants import EntryType
from .core import counters
from .core.scan_identity import discover_source_identity
from .database import Database
from .hashing import compute_quick_hash
from .jobs import JobCancelled, JobPaused, check_cancelled, create_job, update_job
from .models import FileStatRecord
from .path_utils import is_hidden_path, normalize_absolute_path, safe_relative_path

#: Entries staged per transaction when ``scanner.batch_size`` is unset or nonsensical. Large enough
#: that per-entry overhead disappears, small enough that an interrupted scan loses little and other
#: processes see progress promptly.
SCAN_BATCH_SIZE = 5_000

#: Columns staged for each entry, in the order `_row` builds them.
_ENTRY_COLUMNS = (
    "scan_run_id",
    "source_root_id",
    "source_root",
    "absolute_path",
    "relative_path",
    "name",
    "suffix",
    "entry_type",
    "is_hidden",
    "is_symlink",
    "symlink_target",
    "size_bytes",
    "device_id",
    "inode_or_file_id",
    "nlink",
    "mode",
    "created_at",
    "modified_at",
    "scan_status",
    "read_error",
)

# The stat tuple that decides "unchanged". `IS` rather than `=` so two NULLs compare equal —
# with `=` a file whose device id is unknown would look changed on every single scan.
_UNCHANGED = (
    "cur.size_bytes IS old.size_bytes AND cur.modified_at IS old.modified_at "
    "AND cur.device_id IS old.device_id AND cur.inode_or_file_id IS old.inode_or_file_id"
)

# Same composite, as SQL text, for the stored reuse hint (see `stat_fingerprint`).
_FINGERPRINT_SQL = (
    "printf('%s|%s|%s|%s|%s',cur.entry_type,COALESCE(cur.size_bytes,''),"
    "COALESCE(cur.modified_at,''),COALESCE(cur.device_id,''),COALESCE(cur.inode_or_file_id,''))"
)


def build_source_root_fingerprint(source_root: Path) -> str:
    st = source_root.stat()
    filesystem_uuid, _label, metadata = discover_source_identity(source_root)
    return hashlib.sha256(
        f"{filesystem_uuid or ''}|{st.st_dev}|{st.st_ino}|{getattr(st, 'st_rdev', 0)}|{json.dumps(metadata, sort_keys=True)}".encode()
    ).hexdigest()


def stat_fingerprint(record: FileStatRecord) -> str:
    """A reuse hint, never a substitute for a verified content hash.

    A plain composite key rather than a digest. It is only ever compared against another
    fingerprint of the same shape, so hashing bought nothing — and made the value impossible to
    compute inside the set-based diff, where most of them are now produced.
    """
    return (
        f"{record.entry_type}|{record.size_bytes or ''}|{record.modified_at or ''}"
        f"|{record.device_id or ''}|{record.inode_or_file_id or ''}"
    )


class DriveScanner:
    def __init__(self, database: Database, config: AppConfig):
        self.db, self.config = database, config
        # The scan_run id written by the most recent scan() call (set before scan returns), so a
        # caller can scope follow-up analysis to exactly the run just produced rather than guessing
        # via MAX(scan_runs.id) — which is wrong when a scan resumes an earlier interrupted run.
        self.last_run_id: int | None = None

    def inspect_entry(
        self,
        path: Path,
        root: Path,
        dirent: os.DirEntry | None = None,
        relative: str | None = None,
        hidden: bool | None = None,
    ) -> FileStatRecord:
        """Stat one entry. Given the ``DirEntry`` from ``scandir`` this costs a single syscall.

        ``scandir`` already carries the directory-entry type on every platform this runs on, so
        ``is_symlink``/``is_dir``/``is_file`` are answered from cache; only ``stat`` touches the
        filesystem. The old path called ``Path.stat``, ``is_symlink``, ``is_dir`` and ``is_file``
        separately — four syscalls per entry to learn what one already knew.

        ``relative`` and ``hidden`` are carried down the traversal stack. Deriving them here meant
        ``safe_relative_path`` normalising *both* the entry path and the root for every entry, and
        ``is_hidden_path`` walking every path component; the traversal already knows both answers
        for the parent directory, so each is one concatenation or one ``startswith``.
        """
        try:
            source = dirent if dirent is not None else path
            st = source.stat(follow_symlinks=False)
            islink = source.is_symlink()
            counters.count("stat_calls")
            if islink:
                kind = EntryType.SYMLINK
            elif (
                # Path.is_dir/is_file only accept follow_symlinks from 3.13; this package supports
                # 3.11. A DirEntry accepts it everywhere and answers from the cached readdir type.
                dirent.is_dir(follow_symlinks=False) if dirent is not None else path.is_dir()
            ):
                kind = EntryType.DIRECTORY
            elif dirent.is_file(follow_symlinks=False) if dirent is not None else path.is_file():
                kind = EntryType.FILE
            else:
                kind = EntryType.OTHER
            relative_path = Path(relative) if relative is not None else safe_relative_path(path, root)
            return FileStatRecord(
                path,
                relative_path,
                path.name,
                kind,
                st.st_size,
                # Relative, not absolute: a source root under a dotted ancestor
                # (/Volumes/.Backup, ~/.local/share) otherwise marks the entire drive hidden.
                # Stored values are recomputed by the next scan of each source.
                is_hidden_path(relative_path) if hidden is None else hidden,
                islink,
                os.readlink(path) if islink else None,
                st.st_mtime,
                st.st_ctime,
                st.st_mode,
                st.st_dev,
                getattr(st, "st_ino", None),
                getattr(st, "st_nlink", None),
            )
        except OSError as exc:
            return FileStatRecord(
                path,
                Path(relative) if relative is not None else safe_relative_path(path, root),
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
        force_rehash: bool = False,
        job_id: int | None = None,
        parent_job_id: int | None = None,
    ):
        root = normalize_absolute_path(source_root)
        root_fp = build_source_root_fingerprint(root)
        self.db.initialize()
        # `storage_profile: auto` prefers what this source measured last time over the path
        # heuristic. First scan of a drive has no observation and falls back, so nothing waits on it.
        profile = performance_profile(
            self.config, root, self.db.observed_hash_throughput(root_fp)
        )
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
        previous_row = (
            self.db.fetch_one(
                "SELECT id FROM scan_runs WHERE source_root_fingerprint=? AND id<>? AND status='COMPLETE' ORDER BY id DESC LIMIT 1",
                (root_fp, run_id),
            )
            if incremental
            else None
        )
        previous_id = int(previous_row["id"]) if previous_row else None
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
            # Traversal is a single walk: one worker, recorded honestly. `scan_workers` was a knob
            # that only ever changed this number in the job row.
            worker_count=1,
            parent_job_id=parent_job_id,
        )
        update_job(self.db, job_id, "RUNNING")
        counts = {"files": 0, "dirs": 0, "symlinks": 0, "errors": 0, "bytes": 0}
        try:
            self._traverse(root, run_id, source_root_id, job_id, counts)
        except (JobCancelled, JobPaused):
            # A scan that stops early — cancelled outright or paused at a checkpoint — leaves an
            # incomplete inventory; mark the run INTERRUPTED so it is never mistaken for a full scan.
            # (check_cancelled has already settled the job row itself to CANCELLED/PAUSED.)
            self.db.execute("UPDATE scan_runs SET status='INTERRUPTED' WHERE id=?", (run_id,))
            self.db.connect().commit()
            raise
        self._link_parents(run_id)
        self._record_changes(run_id, previous_id, force_rehash)
        # One transaction: a run is COMPLETE exactly when it is this source's current inventory.
        # Splitting these would leave a window in which the newest complete scan is not the one
        # every current-state analyser reads.
        self.db.execute(
            "UPDATE scan_runs SET status='COMPLETE',completed_at=CURRENT_TIMESTAMP WHERE id=?",
            (run_id,),
        )
        self.db.execute(
            "UPDATE source_roots SET latest_complete_scan_run_id=? WHERE id=?",
            (run_id, source_root_id),
        )
        # Same transaction: the views carry the run ids as literals, so they are part of "which
        # snapshot is current" rather than a cache of it.
        self.db.refresh_current_inventory_views()
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
        # After the first completed scan the planner has no statistics; analyse once so the new
        # dashboard indexes are actually chosen. Every scan also settles the WAL a big write left
        # behind so later dashboard reads stay fast.
        completed_scans = self.db.fetch_one(
            "SELECT COUNT(*) n FROM scan_runs WHERE status='COMPLETE'"
        )
        first_scan = int(completed_scans["n"] if completed_scans else 0) == 1
        self.db.optimize_after_write(analyse=first_scan)
        return counts

    # ---------------------------------------------------------------- traversal

    def _traverse(self, root: Path, run_id: int, source_root_id: int, job_id: int, counts: dict) -> None:
        """Walk the tree, staging entries in bounded batches.

        No per-entry query: the traversal only reads the filesystem and appends rows. Parent links
        and change detection are resolved afterwards, set-based, so nothing here needs an id back
        from the database.
        """
        section = self.config.section("scanner")
        excluded_names = frozenset(section.get("excluded_names", []))
        excluded_paths = frozenset(section.get("excluded_paths", []))
        batch_size = max(1, int(section.get("batch_size") or SCAN_BATCH_SIZE))
        # `stay_on_filesystem` means exactly one thing: do not descend into a directory that lives
        # on a different device from the root. The entry is still recorded — a mount point is part
        # of this drive's inventory — but its contents belong to whatever is mounted there.
        root_device = self._device_of(root) if section.get("stay_on_filesystem") else None
        batch: list[tuple] = []
        processed = 0
        # (absolute directory, its path relative to the root, whether it is hidden). "" is the
        # root itself, which is never hidden by virtue of where it happens to be mounted.
        stack: list[tuple[Path, str, bool]] = [(root, "", False)]
        while stack:
            check_cancelled(self.db, job_id)
            directory, rel_dir, dir_hidden = stack.pop()
            try:
                # Sorted so entry ids are reproducible across runs of the same tree; readdir order
                # is not stable. Nothing downstream may depend on id order (see 2.7), but a
                # diffable audit trail is worth one sort per directory.
                entries = sorted(os.scandir(directory), key=lambda e: e.name.casefold())
            except OSError as exc:
                counts["errors"] += 1
                update_job(self.db, job_id, error_count=counts["errors"], current_item=f"{directory}: {exc}")
                continue
            for dent in entries:
                counters.count("entries_enumerated")
                relative = f"{rel_dir}/{dent.name}" if rel_dir else dent.name
                if dent.name in excluded_names or relative in excluded_paths:
                    continue
                path = Path(dent.path)
                hidden = dir_hidden or dent.name.startswith(".")
                rec = self.inspect_entry(path, root, dent, relative, hidden)
                batch.append(self._row(rec, run_id, source_root_id, root, path))
                processed += 1
                if rec.entry_type == EntryType.FILE:
                    counts["files"] += 1
                    counts["bytes"] += rec.size_bytes
                elif rec.entry_type == EntryType.DIRECTORY:
                    counts["dirs"] += 1
                    if root_device is None or rec.device_id == root_device:
                        stack.append((path, relative, hidden))
                elif rec.entry_type == EntryType.SYMLINK:
                    counts["symlinks"] += 1
                if rec.read_error:
                    counts["errors"] += 1
                if len(batch) >= batch_size:
                    self._flush(batch, run_id, counts, processed, job_id, str(directory))
            self._checkpoint_counts(run_id, counts)
        self._flush(batch, run_id, counts, processed, job_id, str(root))

    @staticmethod
    def _device_of(path: Path) -> int | None:
        try:
            return os.stat(path, follow_symlinks=False).st_dev
        except OSError:
            return None

    @staticmethod
    def _row(rec: FileStatRecord, run_id: int, source_root_id: int, root: Path, path: Path) -> tuple:
        return (
            run_id,
            source_root_id,
            str(root),
            str(rec.path),
            str(rec.relative_path),
            rec.name,
            path.suffix.lower(),
            rec.entry_type,
            int(rec.is_hidden),
            int(rec.is_symlink),
            rec.symlink_target,
            rec.size_bytes,
            rec.device_id,
            rec.inode_or_file_id,
            rec.nlink,
            rec.mode,
            rec.created_at,
            rec.modified_at,
            "ERROR" if rec.read_error else "OK",
            rec.read_error,
        )

    def _flush(self, batch: list[tuple], run_id: int, counts: dict, processed: int, job_id: int, current: str) -> None:
        """Write one batch and commit it. This is the scan's unit of durability."""
        if batch:
            columns = ",".join(_ENTRY_COLUMNS)
            marks = ",".join("?" for _ in _ENTRY_COLUMNS)
            updates = ",".join(
                f"{key}=excluded.{key}"
                for key in _ENTRY_COLUMNS
                if key not in {"scan_run_id", "relative_path"}
            )
            # A real upsert, not INSERT OR REPLACE: replacing deletes the row, cascading away its
            # signatures, content links and classifications and reallocating the id, which is how
            # resume used to destroy verified evidence.
            self.db.connect().executemany(
                f"INSERT INTO filesystem_entries({columns}) VALUES({marks}) "
                f"ON CONFLICT(scan_run_id,relative_path) DO UPDATE SET {updates},"
                "last_seen_at=CURRENT_TIMESTAMP",
                batch,
            )
            batch.clear()
        self._checkpoint_counts(run_id, counts)
        self.db.connect().commit()
        update_job(
            self.db,
            job_id,
            processed_count=processed,
            success_count=processed - counts["errors"],
            error_count=counts["errors"],
            current_item=current,
            checkpoint={"scan_run_id": run_id, "processed": processed},
        )
        # A batch that is committed is a safe place to stop, so it is also where a pause/cancel is
        # honoured. The traversal loop above only polls per *directory*, which on a drive with one
        # flat folder of 200k photos is no stop point at all — Cancel then did nothing until the
        # whole tree had been walked.
        check_cancelled(self.db, job_id)

    def _checkpoint_counts(self, run_id: int, counts: dict) -> None:
        self.db.connect().execute(
            "UPDATE scan_runs SET files_seen=?,directories_seen=?,symlinks_seen=?,errors_seen=?,bytes_seen=?,last_checkpoint_at=CURRENT_TIMESTAMP WHERE id=?",
            (*counts.values(), run_id),
        )

    # ---------------------------------------------------------------- set-based diff

    def _link_parents(self, run_id: int) -> None:
        """Resolve every entry's parent by path, in one statement.

        Doing this afterwards is what lets traversal be a pure append: it no longer needs the
        database to hand back an id before it can descend into a subdirectory.
        """
        self.db.connect().execute(
            """UPDATE filesystem_entries SET parent_entry_id=(
                 SELECT p.id FROM filesystem_entries p
                 WHERE p.scan_run_id=filesystem_entries.scan_run_id
                   AND p.relative_path=substr(filesystem_entries.relative_path,1,
                       length(filesystem_entries.relative_path)-length(filesystem_entries.name)-1))
               WHERE scan_run_id=? AND instr(relative_path,'/')>0""",
            (run_id,),
        )
        self.db.connect().commit()

    def _record_changes(self, run_id: int, previous_id: int | None, force_rehash: bool) -> None:
        conn = self.db.connect()
        if previous_id is None:
            conn.commit()
            return
        params = (run_id, previous_id, run_id)
        # 1. What happened to every entry in this run, in one statement instead of one query per
        #    entry. `old.id IS NULL` is NEW; the stat tuple decides UNCHANGED; a file that is
        #    neither is CONTENT_POSSIBLY_CHANGED and a directory is METADATA_CHANGED.
        conn.execute(
            f"""INSERT INTO scan_entry_changes(scan_run_id,entry_id,relative_path,change_status,evidence_json)
                SELECT ?,cur.id,cur.relative_path,
                  CASE WHEN cur.scan_status='ERROR' THEN 'ERROR'
                       WHEN old.id IS NULL THEN 'NEW'
                       WHEN {_UNCHANGED} THEN 'UNCHANGED'
                       WHEN cur.entry_type='file' THEN 'CONTENT_POSSIBLY_CHANGED'
                       ELSE 'METADATA_CHANGED' END,
                  json_object('stat_fingerprint',{_FINGERPRINT_SQL},
                              'prior_entry_id',old.id,'read_error',cur.read_error)
                FROM filesystem_entries cur
                LEFT JOIN filesystem_entries old
                  ON old.scan_run_id=? AND old.relative_path=cur.relative_path
                WHERE cur.scan_run_id=?""",
            params,
        )
        if not force_rehash:
            # 2. Copy forward the verified identity of every file that did not change. This is the
            #    whole point of an incremental scan and it is two statements, not two per file.
            conn.execute(
                f"""INSERT INTO file_signatures(entry_id,quick_hash,full_hash,hash_algorithm,hash_status,hash_error,full_hash_computed_at)
                    SELECT cur.id,s.quick_hash,s.full_hash,s.hash_algorithm,s.hash_status,s.hash_error,s.full_hash_computed_at
                    FROM filesystem_entries cur
                    JOIN filesystem_entries old ON old.scan_run_id=? AND old.relative_path=cur.relative_path
                    JOIN file_signatures s ON s.entry_id=old.id
                    WHERE cur.scan_run_id=? AND cur.entry_type='file' AND s.full_hash IS NOT NULL AND {_UNCHANGED}
                    ON CONFLICT(entry_id) DO UPDATE SET quick_hash=excluded.quick_hash,full_hash=excluded.full_hash,
                      hash_algorithm=excluded.hash_algorithm,hash_status=excluded.hash_status,
                      hash_error=excluded.hash_error,full_hash_computed_at=excluded.full_hash_computed_at""",
                (previous_id, run_id),
            )
            conn.execute(
                f"""INSERT INTO entry_content_links(entry_id,content_object_id,link_status,size_verified,hash_verified,entry_stat_fingerprint)
                    SELECT cur.id,l.content_object_id,COALESCE(l.link_status,'VERIFIED'),1,1,{_FINGERPRINT_SQL}
                    FROM filesystem_entries cur
                    JOIN filesystem_entries old ON old.scan_run_id=? AND old.relative_path=cur.relative_path
                    JOIN file_signatures s ON s.entry_id=old.id
                    JOIN entry_content_links l ON l.entry_id=old.id
                    WHERE cur.scan_run_id=? AND cur.entry_type='file' AND s.full_hash IS NOT NULL AND {_UNCHANGED}
                    ON CONFLICT(entry_id) DO UPDATE SET content_object_id=excluded.content_object_id,
                      link_status=excluded.link_status,entry_stat_fingerprint=excluded.entry_stat_fingerprint,
                      linked_at=CURRENT_TIMESTAMP""",
                (previous_id, run_id),
            )
        # 3. Anything in the previous run with no counterpart here is gone.
        conn.execute(
            """INSERT INTO scan_entry_changes(scan_run_id,entry_id,relative_path,change_status,evidence_json)
               SELECT ?,old.id,old.relative_path,'MISSING',json_object('previous_size',old.size_bytes)
               FROM filesystem_entries old LEFT JOIN filesystem_entries current
               ON current.scan_run_id=? AND current.relative_path=old.relative_path
               WHERE old.scan_run_id=? AND current.id IS NULL""",
            (run_id, run_id, previous_id),
        )
        # 4. A changed or vanished entry invalidates any review decision recorded against it.
        conn.execute(
            f"""UPDATE review_decisions SET stale=1,updated_at=CURRENT_TIMESTAMP
                WHERE target_type='ENTRY' AND current=1 AND target_id IN (
                  SELECT old.id FROM filesystem_entries cur
                  JOIN filesystem_entries old ON old.scan_run_id=? AND old.relative_path=cur.relative_path
                  WHERE cur.scan_run_id=? AND NOT ({_UNCHANGED})
                  UNION ALL
                  SELECT entry_id FROM scan_entry_changes WHERE scan_run_id=? AND change_status='MISSING')""",
            (previous_id, run_id, run_id),
        )
        conn.commit()
        # The only genuinely optional part of the diff: rename detection is a heuristic that reads
        # candidate files to confirm itself, where everything above is bookkeeping the rescan needs.
        if self.config.section("incremental").get("detect_renames", True):
            self._detect_moves(run_id, previous_id, force_rehash)

    def _detect_moves(self, run_id: int, previous_id: int, force_rehash: bool) -> None:
        """Promote NEW entries to move/rename candidates. Evidence only, never content reuse."""
        conn = self.db.connect()
        # Same device+inode+size as something in the previous run that is not still at its old
        # path: the strongest available hint short of a full hash.
        conn.execute(
            """UPDATE scan_entry_changes AS fresh SET change_status='MOVED_OR_RENAMED_CANDIDATE',
               evidence_json=json_object('previous_entry_id',old.id,'previous_relative_path',old.relative_path,
               'same_device_inode',1,'same_size',1)
               FROM filesystem_entries current JOIN filesystem_entries old
               ON old.scan_run_id=? AND old.device_id=current.device_id AND old.inode_or_file_id=current.inode_or_file_id
               AND old.size_bytes=current.size_bytes
               WHERE fresh.scan_run_id=? AND fresh.entry_id=current.id AND fresh.change_status='NEW'
               AND NOT EXISTS(SELECT 1 FROM filesystem_entries existing WHERE existing.scan_run_id=? AND existing.relative_path=old.relative_path)""",
            (previous_id, run_id, run_id),
        )
        # A path that returns after having been recorded MISSING keeps that history explicitly.
        conn.execute(
            """UPDATE scan_entry_changes AS fresh SET
               evidence_json=json_patch(fresh.evidence_json,
                 json_object('reappeared_after_missing',json('true'),'previous_entry_id',(
                   SELECT old.id FROM filesystem_entries old JOIN scan_entry_changes change ON change.entry_id=old.id
                   WHERE old.source_root_id=(SELECT source_root_id FROM filesystem_entries WHERE id=fresh.entry_id)
                     AND old.relative_path=fresh.relative_path AND change.change_status='MISSING'
                   ORDER BY change.id DESC LIMIT 1),
                 'requires_full_hash_confirmation',json('true')))
               WHERE fresh.scan_run_id=? AND fresh.change_status='NEW' AND EXISTS(
                 SELECT 1 FROM filesystem_entries old JOIN scan_entry_changes change ON change.entry_id=old.id
                 WHERE old.source_root_id=(SELECT source_root_id FROM filesystem_entries WHERE id=fresh.entry_id)
                   AND old.relative_path=fresh.relative_path AND change.change_status='MISSING')""",
            (run_id,),
        )
        conn.commit()
        if not force_rehash:
            self._quick_hash_rename_candidates(run_id, previous_id)
        # Content that is verifiably the same as something recorded MISSING in this run is the
        # strongest move evidence there is, so it runs last and overwrites the weaker hints.
        conn.execute(
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
        conn.commit()

    def _quick_hash_rename_candidates(self, run_id: int, previous_id: int) -> None:
        """Quick-hash the few NEW files that could be a rename, and only those.

        Cross-directory moves usually lose inode identity, so a size match against the previous
        run is the funnel. The candidate set is chosen in SQL, so the only files opened are the
        ones a rename could actually explain — the old code hashed a file the first time it saw
        *any* previous-run file of the same size, whatever that size's population.
        """
        section = self.config.section("hashing")
        conn = self.db.connect()
        candidates = self.db.fetch_all(
            """SELECT fresh.entry_id,cur.absolute_path,cur.size_bytes
               FROM scan_entry_changes fresh JOIN filesystem_entries cur ON cur.id=fresh.entry_id
               WHERE fresh.scan_run_id=? AND fresh.change_status='NEW' AND cur.entry_type='file'
                 AND EXISTS(SELECT 1 FROM filesystem_entries old JOIN file_signatures s ON s.entry_id=old.id
                            WHERE old.scan_run_id=? AND old.size_bytes=cur.size_bytes AND s.quick_hash IS NOT NULL)
               ORDER BY fresh.entry_id""",
            (run_id, previous_id),
        )
        for candidate in candidates:
            quick = compute_quick_hash(
                Path(candidate["absolute_path"]),
                section["quick_hash_chunk_bytes"],
                section["quick_hash_middle_samples"],
                section["algorithm"],
            )
            if not quick.stable or not quick.digest:
                continue
            match = self.db.fetch_one(
                """SELECT old.id,old.relative_path FROM filesystem_entries old
                   JOIN file_signatures s ON s.entry_id=old.id
                   WHERE old.scan_run_id=? AND old.size_bytes=? AND s.quick_hash=? ORDER BY old.id LIMIT 1""",
                (previous_id, candidate["size_bytes"], quick.digest),
            )
            if not match:
                continue
            conn.execute(
                "INSERT INTO file_signatures(entry_id,quick_hash,hash_algorithm,hash_status) VALUES(?,?,?,'QUICK_MATCH') "
                "ON CONFLICT(entry_id) DO UPDATE SET quick_hash=excluded.quick_hash,hash_algorithm=excluded.hash_algorithm",
                (candidate["entry_id"], quick.digest, section["algorithm"]),
            )
            conn.execute(
                """UPDATE scan_entry_changes SET change_status='MOVED_OR_RENAMED_CANDIDATE',
                   evidence_json=json_object('previous_entry_id',?,'previous_relative_path',?,
                   'quick_hash_match',json('true'),'requires_full_hash_confirmation',json('true'))
                   WHERE scan_run_id=? AND entry_id=?""",
                (match["id"], match["relative_path"], run_id, candidate["entry_id"]),
            )
        conn.commit()
