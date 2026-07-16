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
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite manifest: {output_path}")
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
    if path.suffix.lower() in {".jsonl", ".json"}:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            result.append(
                ManifestEntry(
                    bool(r.get("approved", False)),
                    int(r["entry_id"]),
                    r["source_path"],
                    r["relative_path"],
                    int(r["size_bytes"]),
                    r.get("expected_sha256", ""),
                    r["classification"],
                    float(r.get("confidence", 0)),
                    r.get("reason_codes", []),
                    r.get("explanation", ""),
                    r.get("canonical_surviving_path"),
                    r.get("reviewer_notes", ""),
                )
            )
        return result
    with path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            result.append(
                ManifestEntry(
                    str(r.get("approved", "")).strip().lower() in {"1", "true", "yes", "y"},
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


def export_decision_manifest(
    database: Database, session_id: int, output_path: Path, format_name: str = "jsonl"
) -> Path:
    rows = database.fetch_all(
        """SELECT e.id,e.absolute_path,e.relative_path,e.size_bytes,c.classification,c.confidence,c.reason_codes_json,c.explanation,s.full_hash,d.decision,d.stale
        FROM review_decisions d JOIN filesystem_entries e ON d.target_type='ENTRY' AND d.target_id=e.id LEFT JOIN classifications c ON c.entry_id=e.id LEFT JOIN file_signatures s ON s.entry_id=e.id
        WHERE d.review_session_id=? AND d.current=1 ORDER BY e.relative_path""",
        (session_id,),
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if output_path.exists():
        raise FileExistsError(f"refusing to overwrite manifest: {output_path}")
    records = [
        {
            "approved": r["decision"] == "APPROVE_FOR_REVIEW" and not r["stale"],
            "entry_id": r["id"],
            "source_path": r["absolute_path"],
            "relative_path": r["relative_path"],
            "size_bytes": r["size_bytes"],
            "expected_sha256": r["full_hash"] or "",
            "classification": r["classification"] or "UNKNOWN",
            "confidence": r["confidence"] or 0,
            "reason_codes": json.loads(r["reason_codes_json"] or "[]"),
            "explanation": r["explanation"] or "",
            "canonical_surviving_path": "",
            "reviewer_notes": "",
            "decision": r["decision"],
            "stale": bool(r["stale"]),
            "review_session_id": session_id,
        }
        for r in rows
    ]
    if format_name == "csv":
        with output_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=FIELDS)
            writer.writeheader()
            for record in records:
                item = dict(record)
                item["reason_codes"] = json.dumps(item["reason_codes"])
                item.pop("decision", None)
                item.pop("stale", None)
                item.pop("review_session_id", None)
                writer.writerow(item)
    else:
        output_path.write_text(
            "".join(json.dumps(record, sort_keys=True) + "\n" for record in records),
            encoding="utf-8",
        )
    return output_path


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
            """SELECT e.absolute_path,e.size_bytes,s.full_hash,s.hash_status
               FROM filesystem_entries e LEFT JOIN file_signatures s ON s.entry_id=e.id WHERE e.id=?""",
            (e.entry_id,),
        )
        if not r:
            errors.append(f"missing entry {e.entry_id}")
        elif r["absolute_path"] != e.source_path or r["size_bytes"] != e.size_bytes:
            errors.append(f"source drift for {e.entry_id}")
        elif e.approved and (r["full_hash"] != e.expected_sha256 or r["hash_status"] not in {"OK", "VERIFIED"}):
            errors.append(f"unverified approved entry {e.entry_id}")
    return errors
