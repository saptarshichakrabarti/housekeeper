"""Near-duplicate document detection: shingle -> MinHash -> LSH candidates -> exact verify.

Candidates come from LSH (no all-pairs comparison); a relationship additionally requires exact
shingle-Jaccard verification, so MinHash alone never authorizes a claim. Results are Tier-5
(probabilistic) and always review-only.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..relationships import upsert_content_relationship
from ..similarity.lsh import candidate_pairs
from ..similarity.minhash import SIGNATURE_VERSION, estimated_jaccard, minhash_signature
from ..similarity.shingling import exact_jaccard, tokenize, word_shingles

ALGORITHM = "text_minhash_lsh"
ALGORITHM_VERSION = "1"
_DOC_SUFFIXES = {".txt", ".md", ".csv", ".rst", ".log", ".docx", ".pdf"}


def _config_fingerprint(config) -> str:
    section = config.section("document_similarity")
    return json.dumps(
        {k: section[k] for k in ("shingle_type", "shingle_size", "minhash_permutations")},
        sort_keys=True,
    )


def _document_text(database, config, content_object_id: int) -> tuple[str, int] | None:
    """Extract normalized text for a content object via a readable representative document."""
    from .documents import extract_document

    for row in database.iter_rows(
        """SELECT e.id,e.absolute_path,e.suffix FROM entry_content_links l JOIN filesystem_entries e ON e.id=l.entry_id
           WHERE l.content_object_id=? AND e.entry_type='file' ORDER BY e.id""",
        (content_object_id,),
    ):
        suffix = (row["suffix"] or "").lower()
        if suffix not in _DOC_SUFFIXES:
            continue
        path = Path(row["absolute_path"])
        if not path.is_file() or path.is_symlink():
            continue
        result = extract_document(path, suffix, config)
        if result.get("extraction_status") == "OK":
            return result.get("normalized_text", ""), int(row["id"])
    return None


def _ensure_document_objects(database, config) -> None:
    """Hash document-suffix files that aren't yet content objects (self-sufficient after a scan)."""
    from ..hashing import compute_full_hash

    algorithm = config.section("hashing")["algorithm"]
    block = config.section("hashing")["full_hash_block_bytes"]
    marks = ",".join("?" for _ in _DOC_SUFFIXES)
    for row in database.iter_rows(
        f"""SELECT e.id,e.absolute_path FROM filesystem_entries e
            LEFT JOIN entry_content_links l ON l.entry_id=e.id
            WHERE e.entry_type='file' AND l.entry_id IS NULL AND lower(e.suffix) IN ({marks})""",
        tuple(_DOC_SUFFIXES),
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


def run_document_minhash_analysis(database, config, scope=None, job_id=None) -> dict[str, int]:
    from ..jobs import check_cancelled, update_job

    section = config.section("document_similarity")
    if not section.get("enabled", True):
        return {"signatures": 0, "candidates": 0, "relationships": 0}
    _ensure_document_objects(database, config)
    num_perm = int(section["minhash_permutations"])
    shingle_size = int(section["shingle_size"])
    lsh_threshold = float(section["lsh_threshold"])
    verify_threshold = float(section["verification_threshold"])
    minimum_tokens = int(section.get("minimum_tokens", 20))
    fingerprint = _config_fingerprint(config)

    allowed = None
    if scope is not None:
        from .scope import scoped_entry_ids

        entry_ids = scoped_entry_ids(database, scope)
        marks = ",".join("?" for _ in entry_ids) or "NULL"
        allowed = {
            int(r["content_object_id"])
            for r in database.fetch_all(
                f"SELECT DISTINCT content_object_id FROM entry_content_links WHERE entry_id IN ({marks})",
                tuple(entry_ids),
            )
        } if entry_ids else set()

    signatures: dict[int, list[int]] = {}
    shingle_sets: dict[int, set[str]] = {}
    counts = {"signatures": 0, "candidates": 0, "relationships": 0}
    objects = database.fetch_all("SELECT id FROM content_objects ORDER BY id")
    for index, obj in enumerate(objects, start=1):
        cid = int(obj["id"])
        if allowed is not None and cid not in allowed:
            continue
        if job_id:
            check_cancelled(database, job_id)
        text = _document_text(database, config, cid)
        if text is None:
            continue
        tokens = tokenize(text[0])
        if len(tokens) < minimum_tokens:
            continue
        shingles = word_shingles(tokens, shingle_size)
        if not shingles:
            continue
        signature = minhash_signature(shingles, num_perm)
        signatures[cid] = signature
        shingle_sets[cid] = shingles
        database.connect().execute(
            """INSERT OR IGNORE INTO similarity_signatures(content_object_id,signature_type,signature_version,configuration_fingerprint,signature_blob,feature_count,status)
               VALUES(?,?,?,?,?,?, 'OK')""",
            (cid, "TEXT_MINHASH", SIGNATURE_VERSION, fingerprint, json.dumps(signature), len(shingles)),
        )
        counts["signatures"] += 1
        if job_id:
            update_job(database, job_id, "RUNNING", processed_count=index)
    database.connect().commit()

    for a_id, b_id in sorted(candidate_pairs(signatures, num_perm, lsh_threshold)):
        counts["candidates"] += 1
        exact = exact_jaccard(shingle_sets[a_id], shingle_sets[b_id])
        if exact < verify_threshold:
            continue  # LSH candidate not confirmed by exact verification
        estimate = estimated_jaccard(signatures[a_id], signatures[b_id])
        relationship = "NEAR_DUPLICATE_DOCUMENT" if exact >= 0.95 else "TEXTUALLY_SIMILAR"
        upsert_content_relationship(
            database,
            "CONTENT_OBJECT",
            a_id,
            "CONTENT_OBJECT",
            b_id,
            relationship,
            "TIER_5_PROBABILISTIC_SIMILARITY",
            exact,
            ALGORITHM,
            ALGORITHM_VERSION,
            fingerprint,
            {"exact_jaccard": round(exact, 4), "minhash_estimate": round(estimate, 4)},
            f"Shingle Jaccard {exact:.0%} (MinHash estimate {estimate:.0%}); review-only.",
        )
        counts["relationships"] += 1
    return counts
