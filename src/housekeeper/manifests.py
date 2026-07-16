import csv
import json
from pathlib import Path

from .database import Database
from .models import ManifestEntry

FIELDS = [
    "approved",
    "entry_id",
    "source_path",
    "relative_path",
    "size_bytes",
    "expected_sha256",
    "classification",
    "confidence",
    "reason_codes",
    "explanation",
    "canonical_surviving_path",
    "reviewer_notes",
]


def export_review_manifest(
    database: Database, output_path: Path, classifications: set[str]
) -> Path:
    rows = database.fetch_all(
        "SELECT e.id,e.absolute_path,e.relative_path,e.size_bytes,c.classification,c.confidence,c.reason_codes_json,c.explanation,c.canonical_entry_id,s.full_hash FROM filesystem_entries e JOIN classifications c ON c.entry_id=e.id LEFT JOIN file_signatures s ON s.entry_id=e.id WHERE c.classification IN (%s) ORDER BY e.relative_path"
        % ",".join("?" * len(classifications)),
        tuple(classifications),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    "approved": "",
                    "entry_id": r["id"],
                    "source_path": r["absolute_path"],
                    "relative_path": r["relative_path"],
                    "size_bytes": r["size_bytes"],
                    "expected_sha256": r["full_hash"] or "",
                    "classification": r["classification"],
                    "confidence": r["confidence"],
                    "reason_codes": r["reason_codes_json"] or "[]",
                    "explanation": r["explanation"] or "",
                    "canonical_surviving_path": "",
                    "reviewer_notes": "",
                }
            )
    return output_path


def load_manifest(path: Path) -> list[ManifestEntry]:
    result = []
    with path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            result.append(
                ManifestEntry(
                    str(r.get("approved", "")).strip().lower()
                    in {"1", "true", "yes", "y"},
                    int(r["entry_id"]),
                    r["source_path"],
                    r["relative_path"],
                    int(r["size_bytes"]),
                    r["expected_sha256"],
                    r["classification"],
                    float(r.get("confidence") or 0),
                    json.loads(r.get("reason_codes") or "[]"),
                    r.get("explanation", ""),
                    r.get("canonical_surviving_path") or None,
                    r.get("reviewer_notes", ""),
                )
            )
    return result


def validate_manifest_schema(entries):
    errors = []
    seen = set()
    for e in entries:
        if e.entry_id in seen:
            errors.append(f"duplicate entry_id {e.entry_id}")
        seen.add(e.entry_id)
        if e.approved and (not e.expected_sha256 or e.size_bytes < 0):
            errors.append(f"invalid approved entry {e.entry_id}")
    return errors


def validate_manifest_against_database(entries, database):
    errors = []
    for e in entries:
        r = database.fetch_one(
            "SELECT absolute_path,size_bytes FROM filesystem_entries WHERE id=?",
            (e.entry_id,),
        )
        if not r:
            errors.append(f"missing entry {e.entry_id}")
        elif r["absolute_path"] != e.source_path or r["size_bytes"] != e.size_bytes:
            errors.append(f"source drift for {e.entry_id}")
    return errors
