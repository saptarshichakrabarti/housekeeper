"""Tabular (CSV/TSV) normalization: row-order-SENSITIVE cell fingerprint.

Detects tables with identical parsed cell content regardless of quoting, line endings, or
trailing whitespace. Row order is preserved by default — row-order-insensitive equivalence is
never assumed automatically, since order is meaningful in most data.
"""

from __future__ import annotations

import csv
import hashlib
import io
from pathlib import Path

from .model import NormalizationProfile, NormalizedArtifact

TABULAR_PROFILE = NormalizationProfile(
    name="TABULAR_CONTENT_EQUIVALENCE",
    content_kind="tabular",
    algorithm="csv_ordered_cell_hash",
    algorithm_version="1",
    configuration={"row_order_sensitive": True},
    loss_characteristics=("quoting", "line_endings", "trailing_whitespace"),
)

_MAX_BYTES = 64 * 1024 * 1024


def normalize_tabular(path: Path, config) -> NormalizedArtifact:
    try:
        if path.stat().st_size > _MAX_BYTES:
            return NormalizedArtifact("UNSUPPORTED", error_code="too_large")
        delimiter = "\t" if path.suffix.lower() == ".tsv" else ","
        text = path.read_text(encoding="utf-8", errors="replace")
        rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
        if not rows:
            return NormalizedArtifact("UNSUPPORTED", error_code="empty")
        digest = hashlib.sha256()
        for row in rows:
            digest.update(("\x1f".join(cell.strip() for cell in row) + "\x1e").encode("utf-8"))
        return NormalizedArtifact(
            status="OK",
            normalized_hash=digest.hexdigest(),
            normalized_size_bytes=len(text),
            structural_fingerprint=hashlib.sha256(f"{len(rows)}x{len(rows[0])}".encode()).hexdigest(),
            artifact={"row_count": len(rows), "column_count": len(rows[0])},
        )
    except (OSError, csv.Error) as exc:
        return NormalizedArtifact("ERROR", error_code=type(exc).__name__, error_message=str(exc))
