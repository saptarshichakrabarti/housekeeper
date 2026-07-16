import json
import shutil
from pathlib import Path

from .hashing import compute_full_hash


def restore_transaction(manifest: Path, dry_run=False):
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
                if compute_full_hash(dst, "sha256", 8_388_608).digest
                != r["expected_hash"]
                else "ALREADY_SATISFIED"
            )
        elif not dry_run:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(src, dst)
            r["restore_status"] = "RESTORED"
        else:
            r["restore_status"] = "DRY_RUN"
        results.append(r)
    return results
