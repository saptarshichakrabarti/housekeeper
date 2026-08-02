"""The shared content-identity service: one place, byte-identical to what the six loops wrote.

Every assertion here is on a property the callers depended on — the digest is the real SHA-256, the
quick digest is a free by-product (never a second read), the result does not depend on how many
worker threads hashed it, and an unreadable file is recorded or skipped exactly as each caller asked.
"""

from __future__ import annotations

import hashlib

from housekeeper.core import counters
from housekeeper.core.identity import ensure_content_identity
from housekeeper.scanner import DriveScanner


def _candidates(database):
    """Unlinked files, shaped as the callers stream them (with inode columns for hard-link reuse)."""
    return database.reader().fetch_all(
        """SELECT e.id,e.scan_run_id,e.absolute_path,e.size_bytes,e.device_id,e.inode_or_file_id
           FROM filesystem_entries e LEFT JOIN entry_content_links l ON l.entry_id=e.id
           WHERE e.entry_type='file' AND l.entry_id IS NULL ORDER BY e.id"""
    )


def _scan(config, database, tmp_path, bodies: dict[str, str]):
    root = tmp_path / "src"
    root.mkdir(parents=True)
    for name, body in bodies.items():
        (root / name).write_text(body, encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    return root


def test_service_writes_real_digests_links_and_signatures(config, database, tmp_path):
    bodies = {f"f{i}.bin": f"content number {i}\n" for i in range(12)}
    _scan(config, database, tmp_path, bodies)

    result = ensure_content_identity(database, config, _candidates(database))
    assert result["hashed"] == len(bodies)
    assert result["errors"] == 0

    for name, body in bodies.items():
        digest = hashlib.sha256(body.encode()).hexdigest()
        row = database.fetch_one(
            """SELECT s.full_hash,s.quick_hash,s.hash_status,co.full_hash AS co_hash
               FROM filesystem_entries e
               JOIN file_signatures s ON s.entry_id=e.id
               JOIN entry_content_links l ON l.entry_id=e.id
               JOIN content_objects co ON co.id=l.content_object_id
               WHERE e.name=?""",
            (name,),
        )
        assert row is not None, f"{name} was not linked"
        assert row["full_hash"] == digest
        assert row["co_hash"] == digest
        assert row["hash_status"] == "OK"
        # The quick digest is a by-product of the same read — populated, not a second pass.
        assert row["quick_hash"]


def test_identity_is_independent_of_worker_count(config, database, tmp_path):
    """Content-object ids may be allocated in any completion order, but the mapping must not change."""
    _scan(config, database, tmp_path, {f"f{i}.bin": f"body {i}\n" for i in range(30)})
    ensure_content_identity(database, config, _candidates(database), workers=1)
    one = {
        (r["name"], r["full_hash"])
        for r in database.fetch_all(
            "SELECT e.name,s.full_hash FROM filesystem_entries e JOIN file_signatures s ON s.entry_id=e.id"
        )
    }

    # A second workspace, hashed with four workers, must produce the identical name→digest mapping.
    from housekeeper.config import load_config
    from housekeeper.database import Database

    other_config = load_config(workspace_override=tmp_path / "ws2")
    other_config.section("performance")["overrides"] = {"full_hash_workers": 4}
    other_db = Database(other_config.database_path)
    other_db.initialize()
    try:
        _scan(other_config, other_db, tmp_path / "b", {f"f{i}.bin": f"body {i}\n" for i in range(30)})
        ensure_content_identity(other_db, other_config, _candidates(other_db), workers=4)
        four = {
            (r["name"], r["full_hash"])
            for r in other_db.fetch_all(
                "SELECT e.name,s.full_hash FROM filesystem_entries e JOIN file_signatures s ON s.entry_id=e.id"
            )
        }
    finally:
        other_db.close()
    assert one == four


def test_identity_reads_each_file_once_and_the_quick_digest_is_free(config, database, tmp_path):
    bodies = {f"f{i}.bin": f"payload {i}\n" for i in range(15)}
    _scan(config, database, tmp_path, bodies)
    with counters.recording() as counts:
        ensure_content_identity(database, config, _candidates(database))
    corpus_bytes = sum(len(b.encode()) for b in bodies.values())
    assert counts["full_hash_bytes"] == corpus_bytes
    assert counts["quick_hash_bytes"] == 0


def test_record_errors_writes_an_error_row_only_when_asked(config, database, tmp_path):
    root = _scan(config, database, tmp_path, {"gone.bin": "will be removed\n", "here.bin": "stays\n"})
    (root / "gone.bin").unlink()  # deleted after the scan recorded it: an unreadable candidate

    # record_errors=False (the content path): the unreadable file is counted and left unsigned.
    silent = ensure_content_identity(database, config, _candidates(database), record_errors=False)
    assert silent["errors"] == 1
    assert silent["hashed"] == 1
    assert database.fetch_one(
        "SELECT COUNT(*) n FROM file_signatures WHERE hash_status='ERROR'"
    )["n"] == 0

    # record_errors=True (the duplicate-candidate path): the same file is recorded as tried.
    loud = ensure_content_identity(database, config, _candidates(database), record_errors=True)
    assert loud["errors"] == 1
    assert database.fetch_one(
        "SELECT COUNT(*) n FROM file_signatures WHERE hash_status='ERROR'"
    )["n"] == 1
