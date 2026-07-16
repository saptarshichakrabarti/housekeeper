import os
import shutil
import uuid
from pathlib import Path

from .database import Database
from .hashing import compute_full_hash
from .models import ManifestEntry
from .path_utils import (is_within, normalize_absolute_path,
                         safe_destination_path)


def validate_review_root(review_root: Path, source_root: Path) -> None:
    if is_within(review_root, source_root) or is_within(source_root, review_root):
        raise ValueError("review root and source root must not contain one another")


def move_approved_entries(
    entries: list[ManifestEntry],
    review_root: Path,
    database: Database,
    dry_run=False,
    yes=False,
) -> Path:
    approved = [e for e in entries if e.approved]
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
                "expected_hash": e.expected_sha256,
                "status": "PLANNED",
            }
            try:
                if not src.is_file() or src.is_symlink():
                    raise ValueError("source is not a regular file")
                h = compute_full_hash(src, "sha256", 8_388_608)
                if (
                    not h.stable
                    or h.size != e.size_bytes
                    or h.digest != e.expected_sha256
                ):
                    raise ValueError("pre-move hash mismatch")
                if dest.exists():
                    raise FileExistsError(str(dest))
                if not dry_run:
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(src, dest)
                    post = compute_full_hash(dest, "sha256", 8_388_608)
                    if post.digest != h.digest:
                        raise IOError("destination verification failed")
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
                "INSERT INTO move_transactions(transaction_run_id,source_entry_id,source_path,destination_path,expected_size,expected_hash,pre_move_hash,post_move_hash,status,error) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    tx.stem,
                    e.entry_id,
                    str(src),
                    str(dest),
                    e.size_bytes,
                    e.expected_sha256,
                    result.get("pre_move_hash"),
                    result.get("post_move_hash"),
                    result["status"],
                    result.get("error"),
                ),
            )
            out.write(__import__("json").dumps(result) + "\n")
    database.connect().commit()
    return tx
