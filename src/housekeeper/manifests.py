import csv
import json
from pathlib import Path

from .constants import LEGACY_HASH_ALGORITHM
from .database import Database
from .hashing import same_hash_algorithm
from .models import ManifestEntry

FIELDS = [
    "approved",
    "entry_id",
    "source_path",
    "relative_path",
    "size_bytes",
    "expected_hash",
    "expected_hash_algorithm",
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
        "SELECT e.id,e.absolute_path,e.relative_path,e.size_bytes,c.classification,c.confidence,c.reason_codes_json,c.explanation,c.canonical_entry_id,s.full_hash,s.hash_algorithm FROM current_entries e JOIN current_classifications c ON c.entry_id=e.id LEFT JOIN file_signatures s ON s.entry_id=e.id WHERE c.classification IN ({}) ORDER BY e.relative_path".format(
            ",".join("?" * len(classifications))
        ),
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
                    "expected_hash": r["full_hash"] or "",
                    "expected_hash_algorithm": r["hash_algorithm"] or LEGACY_HASH_ALGORITHM,
                    "classification": r["classification"],
                    "confidence": r["confidence"],
                    "reason_codes": r["reason_codes_json"] or "[]",
                    "explanation": r["explanation"] or "",
                    "canonical_surviving_path": "",
                    "reviewer_notes": "",
                }
            )
    return output_path


def _declared_hash(record) -> tuple[str, str]:
    """``(digest, algorithm)`` from a manifest row, old or new.

    A manifest written before this field existed carries ``expected_sha256`` and no algorithm, and
    that digest genuinely was SHA-256 — so the fallback is a fact about those files, not a guess.
    """
    digest = record.get("expected_hash") or record.get("expected_sha256") or ""
    return str(digest), str(record.get("expected_hash_algorithm") or LEGACY_HASH_ALGORITHM)


def load_manifest(path: Path) -> list[ManifestEntry]:
    result = []
    if path.suffix.lower() in {".jsonl", ".json"}:
        for line in path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            digest, algorithm = _declared_hash(r)
            result.append(
                ManifestEntry(
                    bool(r.get("approved", False)),
                    int(r["entry_id"]),
                    r["source_path"],
                    r["relative_path"],
                    int(r["size_bytes"]),
                    digest,
                    r["classification"],
                    float(r.get("confidence", 0)),
                    r.get("reason_codes", []),
                    r.get("explanation", ""),
                    r.get("canonical_surviving_path"),
                    r.get("reviewer_notes", ""),
                    algorithm,
                )
            )
        return result
    with path.open(newline="", encoding="utf-8") as f:
        for r in csv.DictReader(f):
            digest, algorithm = _declared_hash(r)
            result.append(
                ManifestEntry(
                    str(r.get("approved", "")).strip().lower() in {"1", "true", "yes", "y"},
                    int(r["entry_id"]),
                    r["source_path"],
                    r["relative_path"],
                    int(r["size_bytes"]),
                    digest,
                    r["classification"],
                    float(r.get("confidence") or 0),
                    json.loads(r.get("reason_codes") or "[]"),
                    r.get("explanation", ""),
                    r.get("canonical_surviving_path") or None,
                    r.get("reviewer_notes", ""),
                    algorithm,
                )
            )
    return result


def export_decision_manifest(
    database: Database, session_id: int, output_path: Path, format_name: str = "jsonl"
) -> Path:
    rows = database.fetch_all(
        """SELECT e.id,e.absolute_path,e.relative_path,e.size_bytes,c.classification,c.confidence,c.reason_codes_json,c.explanation,s.full_hash,s.hash_algorithm,d.decision,d.stale
        FROM review_decisions d JOIN current_entries e ON d.target_type='ENTRY' AND d.target_id=e.id LEFT JOIN current_classifications c ON c.entry_id=e.id LEFT JOIN file_signatures s ON s.entry_id=e.id
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
            "expected_hash": r["full_hash"] or "",
            "expected_hash_algorithm": r["hash_algorithm"] or LEGACY_HASH_ALGORITHM,
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
        if e.approved and (not e.expected_hash or e.size_bytes < 0):
            errors.append(f"invalid approved entry {e.entry_id}")
    return errors


def validate_manifest_against_database(entries, database):
    errors = []
    for e in entries:
        # Deliberately the base table, not current_entries: this resolves an entry id a human
        # already approved in a manifest, which may predate a later rescan. Scoping it would turn
        # "the drive changed under you" into "missing entry", and the drift and hash checks below
        # are what actually decide whether the move is still safe.
        r = database.fetch_one(
            """SELECT e.absolute_path,e.size_bytes,s.full_hash,s.hash_algorithm,s.hash_status
               FROM filesystem_entries e LEFT JOIN file_signatures s ON s.entry_id=e.id WHERE e.id=?""",
            (e.entry_id,),
        )
        if not r:
            errors.append(f"missing entry {e.entry_id}")
        elif r["absolute_path"] != e.source_path or r["size_bytes"] != e.size_bytes:
            errors.append(f"source drift for {e.entry_id}")
        elif e.approved and not same_hash_algorithm(r["hash_algorithm"], e.expected_hash_algorithm):
            # Equal digests under different functions would be a collision, not a match, so this
            # is reported as its own error rather than left to look like an ordinary mismatch.
            errors.append(f"hash algorithm mismatch for {e.entry_id}")
        elif e.approved and (r["full_hash"] != e.expected_hash or r["hash_status"] not in {"OK", "VERIFIED"}):
            errors.append(f"unverified approved entry {e.entry_id}")
    return errors
