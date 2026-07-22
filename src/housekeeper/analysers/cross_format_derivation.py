"""Cross-representation derivation (e.g. DOCX -> PDF, PPTX -> PDF, MD -> HTML).

An editable source and its exported rendering are related but not byte-identical. This is
Tier-6 contextual inference (basename + directory + timestamp evidence), always review-only, and
never authorizes movement. Default guidance: keep the editable source and the final export.
"""

from __future__ import annotations

from pathlib import Path

from ..relationships import upsert_content_relationship
from .document_versions import normalize_version_filename

ALGORITHM = "cross_format_derivation"
ALGORITHM_VERSION = "1"

EDITABLE = {".docx", ".pptx", ".xlsx", ".md", ".odt", ".ipynb", ".tex"}
EXPORT = {".pdf", ".html", ".htm"}


def _ensure_hashed(database, config) -> None:
    from ..hashing import compute_full_hash

    algorithm = config.section("hashing")["algorithm"]
    block = config.section("hashing")["full_hash_block_bytes"]
    suffixes = EDITABLE | EXPORT
    marks = ",".join("?" for _ in suffixes)
    for row in database.iter_rows(
        f"""SELECT e.id,e.absolute_path FROM filesystem_entries e
            LEFT JOIN entry_content_links l ON l.entry_id=e.id
            WHERE e.entry_type='file' AND l.entry_id IS NULL AND lower(e.suffix) IN ({marks})""",
        tuple(suffixes),
    ):
        path = Path(row["absolute_path"])
        if not path.is_file() or path.is_symlink():
            continue
        result = compute_full_hash(path, algorithm, block)
        if not result.stable or not result.digest:
            continue
        cid = database.get_or_create_content_object(algorithm, result.digest, result.size)
        database.connect().execute(
            "INSERT OR REPLACE INTO file_signatures(entry_id,full_hash,hash_algorithm,hash_status,full_hash_computed_at) VALUES(?,?,?, 'VERIFIED', CURRENT_TIMESTAMP)",
            (int(row["id"]), result.digest, algorithm),
        )
        database.link_entry_content(int(row["id"]), cid, "", "VERIFIED")
    database.connect().commit()


def run_cross_format_derivation_analysis(database, config, scope=None, job_id=None) -> dict[str, int]:
    from ..jobs import checkpoint

    _ensure_hashed(database, config)
    # Bucket files by (directory, normalized stem); a bucket with an editable + an export is a
    # candidate derivation pair.
    buckets: dict[tuple[int, str], list[dict]] = {}
    for row in database.iter_rows(
        """SELECT e.id,e.parent_entry_id,e.name,e.suffix,e.modified_at,l.content_object_id
           FROM filesystem_entries e JOIN entry_content_links l ON l.entry_id=e.id
           WHERE e.entry_type='file'"""
    ):
        suffix = (row["suffix"] or "").lower()
        if suffix not in EDITABLE and suffix not in EXPORT:
            continue
        stem = normalize_version_filename(Path(row["name"]).stem)
        key = (int(row["parent_entry_id"] or 0), stem)
        buckets.setdefault(key, []).append(
            {
                "suffix": suffix,
                "content_object_id": int(row["content_object_id"]),
                "modified_at": row["modified_at"],
            }
        )
    counts = {"pairs": 0}
    for bucket_index, members in enumerate(buckets.values(), 1):
        checkpoint(database, job_id, processed_count=bucket_index)
        editables = [m for m in members if m["suffix"] in EDITABLE]
        exports = [m for m in members if m["suffix"] in EXPORT]
        for editable in editables:
            for export in exports:
                if editable["content_object_id"] == export["content_object_id"]:
                    continue
                confidence = 0.75
                evidence = {"same_directory": True, "editable": editable["suffix"], "export": export["suffix"]}
                if editable["modified_at"] and export["modified_at"]:
                    gap = abs(float(export["modified_at"]) - float(editable["modified_at"]))
                    evidence["modified_gap_seconds"] = int(gap)
                    if gap <= 3600:
                        confidence = 0.85
                upsert_content_relationship(
                    database,
                    "CONTENT_OBJECT",
                    editable["content_object_id"],
                    "CONTENT_OBJECT",
                    export["content_object_id"],
                    "LIKELY_EXPORT",
                    "TIER_6_CONTEXTUAL_INFERENCE",
                    confidence,
                    ALGORITHM,
                    ALGORITHM_VERSION,
                    "1",
                    evidence,
                    f"Likely {editable['suffix']} -> {export['suffix']} export (same directory, matching name); review-only.",
                )
                counts["pairs"] += 1
    return counts
