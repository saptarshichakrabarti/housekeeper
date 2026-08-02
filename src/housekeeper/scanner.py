"""Filesystem traversal and incremental diffing.

The unit of work is a **batch**, not an entry. Traversal stages rows into ``filesystem_entries`` a
few thousand at a time, and everything that used to be decided per entry with its own queries —
what changed, which signatures and content links can be reused, what went missing, what looks
renamed — is decided afterwards with a handful of set-based statements. The correct idiom was
already here: the missing-entry epilogue at the bottom of the old loop worked exactly this way.
"""

import hashlib
import itertools
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

#: Above this many entries in a single directory, that directory is streamed unsorted rather than
#: read entirely into memory to sort — a 50M-file flat folder would otherwise be tens of GB of
#: DirEntry objects at once. Reproducible ids (the sort) still hold for every directory under it,
#: which is every real one; a folder this size is pathological and its audit order is not worth an
#: out-of-memory kill.
SCAN_DIR_SORT_LIMIT = 1_000_000

#: Above this many pending directories the resume frontier is not persisted (a null frontier means
#: "re-walk from the root"). A frontier this large means the walk is still early and wide, where a
#: full re-walk costs little relative to the risk of writing a multi-megabyte blob every batch.
FRONTIER_MAX_DIRECTORIES = 100_000

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
        resuming = bool(old and old["status"] != "COMPLETE")
        if resuming:
            assert old is not None  # resuming implies old is set; narrows for the type checker
            run_id = int(old["id"])
        else:
            run_id = self.db.create_scan_run(
                str(root),
                root_fp,
                config_fingerprint(self.config),
                hostname=platform.node(),
                platform=platform.platform(),
                python_version=sys.version,
            )
        self.last_run_id = run_id
        # On resume, continue from the interrupted walk's frontier rather than re-walking the whole
        # tree — the difference between O(remaining) and O(tree) on a multi-day scan. A missing or
        # over-large frontier (or a fresh run) falls back to a full walk from the root.
        initial_stack = self._resume_stack(root, run_id) if resuming else None
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
            # scandir+stat release the GIL, so a small pool overlaps a walk's I/O on fast or
            # high-latency storage; hdd stays at one worker. Recorded honestly on the job row.
            worker_count=int(profile.get("traversal_workers", 1)),
            parent_job_id=parent_job_id,
        )
        update_job(self.db, job_id, "RUNNING")
        counts = {"files": 0, "dirs": 0, "symlinks": 0, "errors": 0, "bytes": 0}
        traversal_workers = max(1, int(profile.get("traversal_workers", 1)))
        try:
            if traversal_workers > 1:
                self._traverse_parallel(
                    root, run_id, source_root_id, job_id, counts, initial_stack, traversal_workers
                )
            else:
                self._traverse(root, run_id, source_root_id, job_id, counts, initial_stack)
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

    @staticmethod
    def _read_directory(directory: Path, sort_limit: int):
        """Return ``(entries, overflow)`` for one directory.

        Within ``sort_limit`` a sorted list (so entry ids stay reproducible across runs); beyond it
        the raw ``scandir`` iterator, streamed unsorted, so a pathologically large flat directory is
        never fully materialised. May raise ``OSError`` exactly as ``os.scandir`` does.
        """
        iterator = os.scandir(directory)
        buffered: list[os.DirEntry] = []
        for dent in iterator:
            buffered.append(dent)
            if len(buffered) >= sort_limit:
                # Chain the buffered head with the un-consumed tail; the caller streams both. The
                # scandir handle closes when the chain is exhausted (or on GC if the walk stops).
                return itertools.chain(buffered, iterator), True
        iterator.close()
        buffered.sort(key=lambda e: e.name.casefold())
        return buffered, False

    @staticmethod
    def _identity(device: int | None, inode: int | None) -> tuple[int, int] | None:
        """A directory's ``(device, inode)`` identity, or ``None`` when either is unknown."""
        return (device, inode) if device is not None and inode is not None else None

    def _resume_stack(self, root: Path, run_id: int) -> list[tuple[Path, str, bool]] | None:
        """Reconstruct the pending-directory stack from a resumed run's persisted frontier.

        ``None`` when there is no usable frontier (a first interruption before any batch, an
        over-large frontier stored as NULL, or a corrupt value), which makes the caller fall back to
        a full re-walk from the root — correct, just not incremental.
        """
        row = self.db.fetch_one("SELECT frontier_json FROM scan_runs WHERE id=?", (run_id,))
        if not row or not row["frontier_json"]:
            return None
        try:
            frontier = json.loads(row["frontier_json"])
        except (TypeError, ValueError):
            return None
        if not isinstance(frontier, list) or not frontier:
            return None
        # rel is relative to root; `root / ""` is root itself, so this reconstructs the absolute
        # directory for every frontier entry including the root.
        return [((root / rel), str(rel), bool(hidden)) for rel, hidden in frontier]

    def _traverse(
        self,
        root: Path,
        run_id: int,
        source_root_id: int,
        job_id: int,
        counts: dict,
        initial_stack: list[tuple[Path, str, bool]] | None = None,
    ) -> None:
        """Walk the tree, staging entries in bounded batches.

        No per-entry query: the traversal only reads the filesystem and appends rows. Parent links
        and change detection are resolved afterwards, set-based, so nothing here needs an id back
        from the database.

        ``initial_stack`` seeds the pending directories from a resumed run's frontier, so a scan
        interrupted on day two continues from where it stopped rather than re-walking day one.
        """
        section = self.config.section("scanner")
        excluded_names = frozenset(section.get("excluded_names", []))
        excluded_paths = frozenset(section.get("excluded_paths", []))
        batch_size = max(1, int(section.get("batch_size") or SCAN_BATCH_SIZE))
        error_limit = int(section.get("pause_after_consecutive_errors", 0) or 0)
        cycle_cap = max(0, int(section.get("cycle_guard_max_directories", 0) or 0))
        # `stay_on_filesystem` means exactly one thing: do not descend into a directory that lives
        # on a different device from the root. The entry is still recorded — a mount point is part
        # of this drive's inventory — but its contents belong to whatever is mounted there.
        root_device = self._device_of(root) if section.get("stay_on_filesystem") else None
        batch: list[tuple] = []
        processed = 0
        # A run of consecutive unreadable entries is the signature of a drive that dropped mid-scan.
        # Reset by any readable entry, so a few scattered permission errors never trip the breaker.
        consecutive_errors = 0
        cap_noted = False
        # Directory identities already entered, so a bind mount or crafted symlink loop *within one
        # filesystem* cannot be walked forever — stay_on_filesystem only stops cross-device loops.
        visited: set[tuple[int, int]] = set()
        root_identity = self._identity(*self._device_and_inode(root))
        if root_identity is not None:
            visited.add(root_identity)
        # (absolute directory, its path relative to the root, whether it is hidden). "" is the
        # root itself, which is never hidden by virtue of where it happens to be mounted.
        stack: list[tuple[Path, str, bool]] = (
            list(initial_stack) if initial_stack is not None else [(root, "", False)]
        )
        while stack:
            check_cancelled(self.db, job_id)
            directory, rel_dir, dir_hidden = stack.pop()
            # The resume frontier for this directory: everything still pending, plus this directory
            # itself (last, so it is re-walked first). Captured *before* the directory is read, so it
            # excludes the directory's own children — a resume re-derives those by re-walking, and
            # nothing already on the stack is walked twice.
            frontier = [(rd, dh) for (_p, rd, dh) in stack] + [(rel_dir, dir_hidden)]
            try:
                entries, overflow = self._read_directory(directory, SCAN_DIR_SORT_LIMIT)
            except OSError as exc:
                counts["errors"] += 1
                consecutive_errors += 1
                update_job(self.db, job_id, error_count=counts["errors"], current_item=f"{directory}: {exc}")
                if error_limit and consecutive_errors >= error_limit:
                    self._pause_on_error_storm(batch, run_id, counts, processed, job_id, directory, consecutive_errors, frontier)
                continue
            if overflow:
                counters.count("directories_streamed_unsorted")
                update_job(self.db, job_id, current_item=f"{directory}: >{SCAN_DIR_SORT_LIMIT} entries, streamed unsorted")
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
                if rec.read_error:
                    counts["errors"] += 1
                    consecutive_errors += 1
                else:
                    consecutive_errors = 0
                if rec.entry_type == EntryType.FILE:
                    counts["files"] += 1
                    counts["bytes"] += rec.size_bytes
                elif rec.entry_type == EntryType.DIRECTORY:
                    counts["dirs"] += 1
                    descend = root_device is None or rec.device_id == root_device
                    identity = self._identity(rec.device_id, rec.inode_or_file_id)
                    if descend and identity is not None and identity in visited:
                        counters.count("directories_skipped_as_cycles")
                        descend = False
                    if descend:
                        if identity is not None and cycle_cap:
                            if len(visited) < cycle_cap:
                                visited.add(identity)
                            elif not cap_noted:
                                cap_noted = True
                                update_job(self.db, job_id, current_item=f"cycle guard cap reached ({cycle_cap} dirs); descending unguarded")
                        stack.append((path, relative, hidden))
                elif rec.entry_type == EntryType.SYMLINK:
                    counts["symlinks"] += 1
                if len(batch) >= batch_size:
                    self._flush(batch, run_id, counts, processed, job_id, str(directory), frontier)
                if error_limit and consecutive_errors >= error_limit:
                    self._pause_on_error_storm(batch, run_id, counts, processed, job_id, directory, consecutive_errors, frontier)
            self._checkpoint_counts(run_id, counts)
        # The walk is complete: clear the frontier (an empty list stores NULL) so a resumed reopen of
        # this run — should one ever happen before it is marked COMPLETE — starts clean.
        self._flush(batch, run_id, counts, processed, job_id, str(root), [])

    def _pause_on_error_storm(self, batch, run_id, counts, processed, job_id, where, consecutive, frontier=None) -> None:
        """Commit what has been read, park the scan as PAUSED, and stop — instead of walking a dead
        drive to the end. Resume re-walks safely (the entry upsert is idempotent)."""
        self._flush(batch, run_id, counts, processed, job_id, str(where), frontier)
        update_job(
            self.db,
            job_id,
            "PAUSED",
            current_item=f"paused after {consecutive} consecutive read errors near {where}",
        )
        raise JobPaused(f"scan paused after {consecutive} consecutive read errors")

    # ------------------------------------------------------------ parallel traversal (opt-in)

    def _scan_directory_for_worker(
        self,
        dir_tuple: tuple[Path, str, bool],
        root: Path,
        excluded_names: frozenset[str],
        excluded_paths: frozenset[str],
    ):
        """Read one directory — the pure-I/O half of the walk, run on a worker thread.

        Touches no database and no shared mutable state: it only reads the filesystem (``scandir`` +
        ``stat``, which release the GIL) and returns this directory's records for the main thread to
        stage. Counters are process-global and lock-guarded, so enumeration and stat counts are
        recorded here exactly as the serial walk records them.
        """
        directory, rel_dir, dir_hidden = dir_tuple
        try:
            entries, overflow = self._read_directory(directory, SCAN_DIR_SORT_LIMIT)
        except OSError as exc:
            return [], False, str(exc)
        listing: list[tuple[FileStatRecord, str, bool, Path]] = []
        for dent in entries:
            counters.count("entries_enumerated")
            relative = f"{rel_dir}/{dent.name}" if rel_dir else dent.name
            if dent.name in excluded_names or relative in excluded_paths:
                continue
            path = Path(dent.path)
            hidden = dir_hidden or dent.name.startswith(".")
            listing.append((self.inspect_entry(path, root, dent, relative, hidden), relative, hidden, path))
        return listing, overflow, None

    def _traverse_parallel(
        self,
        root: Path,
        run_id: int,
        source_root_id: int,
        job_id: int,
        counts: dict,
        initial_stack: list[tuple[Path, str, bool]] | None,
        workers: int,
    ) -> None:
        """Walk the tree with ``workers`` directory readers, all bookkeeping on this thread.

        Only ``scandir``/``stat`` is offloaded; the single writer, the batch, the cycle guard and the
        frontier stay here, so there is exactly one representation of scan state and no SQLite
        connection ever crosses a thread. At ``workers == 1`` the serial walk is used instead, so
        this path is purely additive.

        The resume frontier here is everything not yet durably committed — queued directories, those
        in flight, and those whose records sit in the unflushed batch. A resume re-walks all of them;
        the entry upsert makes any resulting re-walk idempotent, so the cost is bounded redundancy,
        never a lost or duplicated row.
        """
        from collections import deque
        from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait

        section = self.config.section("scanner")
        excluded_names = frozenset(section.get("excluded_names", []))
        excluded_paths = frozenset(section.get("excluded_paths", []))
        batch_size = max(1, int(section.get("batch_size") or SCAN_BATCH_SIZE))
        error_limit = int(section.get("pause_after_consecutive_errors", 0) or 0)
        cycle_cap = max(0, int(section.get("cycle_guard_max_directories", 0) or 0))
        root_device = self._device_of(root) if section.get("stay_on_filesystem") else None

        batch: list[tuple] = []
        processed = 0
        consecutive_errors = 0
        cap_noted = False
        visited: set[tuple[int, int]] = set()
        root_identity = self._identity(*self._device_and_inode(root))
        if root_identity is not None:
            visited.add(root_identity)
        pending: deque[tuple[Path, str, bool]] = deque(
            initial_stack if initial_stack is not None else [(root, "", False)]
        )
        inflight: dict = {}
        # (rel, hidden) of directories whose records are in the current unflushed batch.
        unflushed: list[tuple[str, bool]] = []

        def frontier() -> list[tuple[str, bool]]:
            return (
                [(rd, dh) for (_p, rd, dh) in pending]
                + [(rd, dh) for (_p, rd, dh) in inflight.values()]
                + list(unflushed)
            )

        def flush(current: str) -> None:
            self._flush(batch, run_id, counts, processed, job_id, current, frontier())
            unflushed.clear()

        with ThreadPoolExecutor(max_workers=workers) as pool:
            try:
                while pending or inflight:
                    check_cancelled(self.db, job_id)
                    while pending and len(inflight) < workers:
                        dir_tuple = pending.popleft()
                        future = pool.submit(
                            self._scan_directory_for_worker,
                            dir_tuple,
                            root,
                            excluded_names,
                            excluded_paths,
                        )
                        inflight[future] = dir_tuple
                    done, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
                    for future in done:
                        directory, rel_dir, dir_hidden = inflight.pop(future)
                        listing, overflow, error = future.result()
                        if error is not None:
                            counts["errors"] += 1
                            consecutive_errors += 1
                            update_job(self.db, job_id, error_count=counts["errors"], current_item=f"{directory}: {error}")
                            if error_limit and consecutive_errors >= error_limit:
                                self._pause_on_error_storm(batch, run_id, counts, processed, job_id, directory, consecutive_errors, frontier())
                            continue
                        if overflow:
                            counters.count("directories_streamed_unsorted")
                        # This directory's records are now in the batch: keep it in the frontier until
                        # the batch is flushed, and enqueue its children only after it is processed
                        # (so a flush mid-directory never lists a child that will be rediscovered).
                        unflushed.append((rel_dir, dir_hidden))
                        children: list[tuple[Path, str, bool]] = []
                        for rec, relative, hidden, path in listing:
                            batch.append(self._row(rec, run_id, source_root_id, root, path))
                            processed += 1
                            if rec.read_error:
                                counts["errors"] += 1
                                consecutive_errors += 1
                            else:
                                consecutive_errors = 0
                            if rec.entry_type == EntryType.FILE:
                                counts["files"] += 1
                                counts["bytes"] += rec.size_bytes
                            elif rec.entry_type == EntryType.DIRECTORY:
                                counts["dirs"] += 1
                                descend = root_device is None or rec.device_id == root_device
                                identity = self._identity(rec.device_id, rec.inode_or_file_id)
                                if descend and identity is not None and identity in visited:
                                    counters.count("directories_skipped_as_cycles")
                                    descend = False
                                if descend:
                                    if identity is not None and cycle_cap:
                                        if len(visited) < cycle_cap:
                                            visited.add(identity)
                                        elif not cap_noted:
                                            cap_noted = True
                                            update_job(self.db, job_id, current_item=f"cycle guard cap reached ({cycle_cap} dirs); descending unguarded")
                                    children.append((path, relative, hidden))
                            elif rec.entry_type == EntryType.SYMLINK:
                                counts["symlinks"] += 1
                            if len(batch) >= batch_size:
                                flush(str(directory))
                            if error_limit and consecutive_errors >= error_limit:
                                self._pause_on_error_storm(batch, run_id, counts, processed, job_id, directory, consecutive_errors, frontier())
                        pending.extend(children)
                    self._checkpoint_counts(run_id, counts)
            except (JobCancelled, JobPaused):
                pool.shutdown(wait=False, cancel_futures=True)
                raise
        # The walk is complete: clear the frontier (empty list stores NULL).
        self._flush(batch, run_id, counts, processed, job_id, str(root), [])

    @staticmethod
    def _device_and_inode(path: Path) -> tuple[int | None, int | None]:
        try:
            st = os.stat(path, follow_symlinks=False)
            return st.st_dev, getattr(st, "st_ino", None)
        except OSError:
            return None, None

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

    def _flush(
        self,
        batch: list[tuple],
        run_id: int,
        counts: dict,
        processed: int,
        job_id: int,
        current: str,
        frontier: list[tuple[str, bool]] | None = None,
    ) -> None:
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
        # Persist the resume frontier at the same cadence as durability. `frontier` is the pending
        # directories captured when the current directory was popped — it excludes that directory's
        # own not-yet-discovered children, so a resume re-walks the current directory to rediscover
        # them without double-walking anything already on the stack. Too large a frontier stores NULL
        # (a full re-walk), and None here means "leave whatever was last written".
        if frontier is not None:
            payload = (
                json.dumps(frontier) if 0 < len(frontier) <= FRONTIER_MAX_DIRECTORIES else None
            )
            self.db.connect().execute(
                "UPDATE scan_runs SET frontier_json=? WHERE id=?", (payload, run_id)
            )
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
