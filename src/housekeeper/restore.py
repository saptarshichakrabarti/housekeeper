import json
import os
import shutil
from pathlib import Path

from .hashing import compute_full_hash


def verify_transaction(manifest: Path) -> list[dict]:
    """Re-hash every moved file at its destination to confirm the review copy is intact.

    Operates on a transaction manifest (as written by ``move-to-review``), never a review
    manifest.  Each moved record is verified independently and never modified.
    """
    results = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        record = json.loads(line)
        if record.get("status") != "MOVED":
            record["verify_status"] = "SKIPPED"
        else:
            destination = Path(record["destination_path"])
            if not destination.is_file():
                record["verify_status"] = "MISSING_DESTINATION"
            elif (
                compute_full_hash(destination, "sha256", 8_388_608).digest
                != record.get("expected_hash")
            ):
                record["verify_status"] = "HASH_MISMATCH"
            else:
                record["verify_status"] = "VERIFIED"
        results.append(record)
    return results


def restore_transaction(manifest: Path, dry_run=False, yes=False):
    results = []
    for line in manifest.read_text(encoding="utf-8").splitlines():
        r = json.loads(line)
        if r.get("status") != "MOVED":
            continue
        src, dst = Path(r["destination_path"]), Path(r["source_path"])
        if not src.is_file():
            r["restore_status"] = "MISSING_REVIEW_COPY"
        elif compute_full_hash(src, "sha256", 8_388_608).digest != r["expected_hash"]:
            r["restore_status"] = "HASH_MISMATCH"
        elif dst.exists():
            r["restore_status"] = (
                "DESTINATION_EXISTS"
                if compute_full_hash(dst, "sha256", 8_388_608).digest != r["expected_hash"]
                else "ALREADY_SATISFIED"
            )
        elif not dry_run and not yes:
            r["restore_status"] = "CONFIRMATION_REQUIRED"
        elif not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            # Copy, verify, then remove the review copy; never replace an existing path.
            shutil.copy2(src, dst)
            restored = compute_full_hash(dst, "sha256", 8_388_608)
            if restored.digest != r["expected_hash"]:
                dst.unlink(missing_ok=True)
                r["restore_status"] = "DESTINATION_VERIFICATION_FAILED"
            else:
                os.unlink(src)
                r["restore_status"] = "RESTORED"
        else:
            r["restore_status"] = "DRY_RUN"
        results.append(r)
    return results
