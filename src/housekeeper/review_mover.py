import json
import os
import shutil
import uuid
from pathlib import Path

from .constants import LEGACY_HASH_ALGORITHM
from .database import Database
from .hashing import compute_full_hash, same_hash_algorithm, verify_file_against_manifest
from .models import ManifestEntry
from .path_utils import is_within, normalize_absolute_path, safe_destination_path


def validate_review_root(review_root: Path, source_root: Path) -> None:
    if is_within(review_root, source_root) or is_within(source_root, review_root):
        raise ValueError("review root and source root must not contain one another")


def move_approved_entries(
    entries: list[ManifestEntry],
    review_root: Path,
    database: Database,
    dry_run=False,
    yes=False,
    manifest_path: Path | None = None,
) -> Path:
    approved = [e for e in entries if e.approved]
    if approved and not dry_run and not yes:
        raise ValueError("refusing to move without --yes; use --dry-run first")
    # Preflight the immutable manifest against the current database before any copy starts.
    approved_ids = {entry.entry_id for entry in approved}
    survivor_cache: dict[int, bool] = {}
    for e in approved:
        row = database.fetch_one(
            """SELECT e.source_root,e.absolute_path,e.size_bytes,s.full_hash,s.hash_algorithm,s.hash_status
               FROM filesystem_entries e LEFT JOIN file_signatures s ON s.entry_id=e.id WHERE e.id=?""",
            (e.entry_id,),
        )
        if not row or row["absolute_path"] != e.source_path or row["size_bytes"] != e.size_bytes:
            raise ValueError(f"manifest/database drift for entry {e.entry_id}")
        if not same_hash_algorithm(row["hash_algorithm"], e.expected_hash_algorithm):
            raise ValueError(
                f"manifest hash algorithm {e.expected_hash_algorithm} does not match the recorded "
                f"{row['hash_algorithm']} for entry {e.entry_id}"
            )
        if row["full_hash"] != e.expected_hash or row["hash_status"] not in {"OK", "VERIFIED"}:
            raise ValueError(f"manifest is not backed by a verified current hash for entry {e.entry_id}")
        validate_review_root(normalize_absolute_path(review_root), normalize_absolute_path(Path(row["source_root"])))
        content = database.fetch_one(
            "SELECT content_object_id FROM entry_content_links WHERE entry_id=? AND link_status='VERIFIED' AND hash_verified=1",
            (e.entry_id,),
        )
        if content and content["content_object_id"] not in survivor_cache:
            survivors = database.iter_rows(
                """SELECT e.id,e.absolute_path,co.full_hash,co.hash_algorithm FROM entry_content_links l
                   JOIN filesystem_entries e ON e.id=l.entry_id JOIN content_objects co ON co.id=l.content_object_id
                   WHERE l.content_object_id=? AND l.link_status='VERIFIED' AND l.hash_verified=1""",
                (content["content_object_id"],),
            )
            # Re-hashed with the content object's own algorithm: a surviving copy proved with the
            # wrong function would fail to match and this would refuse a legitimate move.
            survivor_cache[content["content_object_id"]] = any(
                candidate["id"] not in approved_ids
                and Path(candidate["absolute_path"]).is_file()
                and not Path(candidate["absolute_path"]).is_symlink()
                and compute_full_hash(
                    Path(candidate["absolute_path"]),
                    candidate["hash_algorithm"] or LEGACY_HASH_ALGORITHM,
                    8_388_608,
                ).digest
                == candidate["full_hash"]
                for candidate in survivors
            )
        occurrence = database.fetch_one(
            "SELECT COUNT(*) AS n FROM entry_content_links WHERE content_object_id=? AND link_status='VERIFIED' AND hash_verified=1",
            (content["content_object_id"],),
        ) if content else None
        occurrence_count = int(occurrence["n"]) if occurrence else 0
        if content and occurrence_count > 1 and not survivor_cache[content["content_object_id"]]:
            raise ValueError("refusing to move the last verified copy of a content object")
    if manifest_path is not None:
        _verify_exported_snapshot(database, manifest_path)
    tx = review_root.parent / f"transaction-{uuid.uuid4().hex}.jsonl"
    tx.parent.mkdir(parents=True, exist_ok=True)
    with tx.open("w", encoding="utf-8") as out:
        for e in approved:
            dest = safe_destination_path(review_root, Path(e.relative_path))
            src = normalize_absolute_path(Path(e.source_path))
            result = {
                "entry_id": e.entry_id,
                "source_path": str(src),
                "destination_path": str(dest),
                "expected_size": e.size_bytes,
                "expected_hash": e.expected_hash,
                # Recorded so `verify-transaction` and `restore` re-hash with the same function
                # years later, whatever the configured default has become by then.
                "expected_hash_algorithm": e.expected_hash_algorithm,
                "status": "PLANNED",
            }
            try:
                if not src.is_file() or src.is_symlink():
                    raise ValueError("source is not a regular file")
                h = verify_file_against_manifest(
                    src, e.size_bytes, e.expected_hash, e.expected_hash_algorithm
                )
                if dest.exists():
                    raise FileExistsError(str(dest))
                if not dry_run:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                    post = compute_full_hash(dest, e.expected_hash_algorithm, 8_388_608)
                    if post.digest != h.digest:
                        raise OSError("destination verification failed")
                    os.unlink(src)
                    result.update(
                        status="MOVED",
                        pre_move_hash=h.digest,
                        post_move_hash=post.digest,
                    )
                else:
                    result["status"] = "DRY_RUN"
            except (OSError, ValueError) as exc:
                result.update(status="FAILED", error=str(exc))
            database.connect().execute(
                "INSERT INTO move_transactions(transaction_run_id,source_entry_id,source_path,destination_path,expected_size,expected_hash,expected_hash_algorithm,pre_move_hash,post_move_hash,status,error) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    tx.stem,
                    e.entry_id,
                    str(src),
                    str(dest),
                    e.size_bytes,
                    e.expected_hash,
                    e.expected_hash_algorithm,
                    result.get("pre_move_hash"),
                    result.get("post_move_hash"),
                    result["status"],
                    result.get("error"),
                ),
            )
            out.write(__import__("json").dumps(result) + "\n")
    database.connect().commit()
    return tx


def _verify_exported_snapshot(database: Database, manifest_path: Path) -> None:
    """Decision manifests are executable only when their exported snapshot still matches."""
    if not manifest_path.is_file() or manifest_path.suffix.lower() not in {".jsonl", ".json"}:
        return  # Legacy CSV manifests remain supported through the normal hash preflight.
    records = [json.loads(line) for line in manifest_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    session_ids = {record.get("review_session_id") for record in records if record.get("review_session_id")}
    if not session_ids:
        return
    if len(session_ids) != 1:
        raise ValueError("manifest contains more than one review session")
    digest = __import__("hashlib").sha256(manifest_path.read_bytes()).hexdigest()
    for row in database.iter_rows("SELECT snapshot_json FROM review_snapshots"):
        payload = json.loads(row["snapshot_json"])
        if payload.get("session_id") in session_ids and payload.get("manifest_hash") == digest:
            return
    raise ValueError("decision manifest is not backed by a matching immutable review snapshot")
