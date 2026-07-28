"""CSV and JSONL recommendation exports (facts + reason codes, never a delete instruction)."""

from __future__ import annotations

import csv
import json
from pathlib import Path

from .formatting import display_path, redacts_paths

_FIELDS = [
    "entry_id",
    "relative_path",
    "absolute_path",
    "classification",
    "confidence",
    "primary_reason_code",
    "reason_codes",
    "size_bytes",
    "canonical_entry_id",
    "requires_manual_approval",
    "explanation",
]


def _recommendation_rows(database, redact: bool = False):
    for row in database.iter_rows(
        """SELECT c.entry_id,e.relative_path,e.absolute_path,c.classification,c.confidence,
                  c.primary_reason_code,c.reason_codes_json,e.size_bytes,c.canonical_entry_id,
                  c.requires_manual_approval,c.explanation
           FROM current_classifications c JOIN current_entries e ON e.id=c.entry_id
           WHERE c.classification LIKE 'REVIEW_%' ORDER BY e.size_bytes DESC"""
    ):
        yield {
            "entry_id": row["entry_id"],
            "relative_path": row["relative_path"],
            "absolute_path": display_path(row["absolute_path"], row["relative_path"], redact),
            "classification": row["classification"],
            "confidence": row["confidence"],
            "primary_reason_code": row["primary_reason_code"],
            "reason_codes": json.loads(row["reason_codes_json"] or "[]"),
            "size_bytes": row["size_bytes"],
            "canonical_entry_id": row["canonical_entry_id"],
            "requires_manual_approval": row["requires_manual_approval"],
            "explanation": row["explanation"],
        }


def export_csv(database, output_path: Path, config=None) -> Path:
    """Recommendations as CSV. ``config`` is optional so a caller can export without one; passing it
    is what enables `reporting.redact_source_paths`."""
    redact = redacts_paths(config) if config is not None else False
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=_FIELDS)
        writer.writeheader()
        for record in _recommendation_rows(database, redact):
            item = dict(record)
            item["reason_codes"] = json.dumps(item["reason_codes"])
            writer.writerow(item)
    return output_path


def export_jsonl(database, output_path: Path, config=None) -> Path:
    redact = redacts_paths(config) if config is not None else False
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as handle:
        for record in _recommendation_rows(database, redact):
            handle.write(json.dumps(record, sort_keys=True) + "\n")
    return output_path
