"""Near-duplicate document detection: shingle -> MinHash -> LSH candidates -> exact verify.

Candidates come from LSH (no all-pairs comparison); a relationship additionally requires exact
shingle-Jaccard verification, so MinHash alone never authorizes a claim. Results are Tier-5
(probabilistic) and always review-only.
"""

from __future__ import annotations

import json
from pathlib import Path

from ..relationships import invalidate_content_relationships, upsert_content_relationship
from ..similarity.lsh import candidate_pairs
from ..similarity.minhash import SIGNATURE_VERSION, estimated_jaccard, minhash_signature
from ..similarity.shingling import exact_jaccard, tokenize, word_shingles

ALGORITHM = "text_minhash_lsh"
ALGORITHM_VERSION = "1"
_DOC_SUFFIXES = {".txt", ".md", ".csv", ".rst", ".log", ".docx", ".pdf"}


def _config_fingerprint(config) -> str:
    section = config.section("document_similarity")
    return json.dumps(
        {
            key: section[key]
            for key in (
                "shingle_type",
                "shingle_size",
                "minhash_permutations",
                "lsh_threshold",
                "verification_threshold",
                "minimum_tokens",
            )
        },
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


def _ensure_document_objects(database, config, scope) -> None:
    """Hash document-suffix files that aren't yet content objects (self-sufficient after a scan)."""
    from ..core.identity import ensure_content_identity, stream_identity_candidates

    marks = ",".join("?" for _ in _DOC_SUFFIXES)
    entry_sql, entry_params = scope.entry_id_sql()
    stream = stream_identity_candidates(
        database.reader(),
        f"""SELECT e.id,e.scan_run_id,e.absolute_path,e.device_id,e.inode_or_file_id,e.nlink
            FROM filesystem_entries e LEFT JOIN entry_content_links l ON l.entry_id=e.id
            WHERE e.entry_type='file' AND l.entry_id IS NULL AND e.id IN ({entry_sql})
              AND lower(e.suffix) IN ({marks}){{keyset}}""",
        (*entry_params, *_DOC_SUFFIXES),
    )
    ensure_content_identity(database, config, stream, progress_phase="hashing documents")


def run_document_minhash_analysis(
    database,
    config,
    scope=None,
    job_id=None,
    maximum_documents: int | None = None,
    maximum_tokens: int | None = None,
) -> dict:
    from ..jobs import check_cancelled, checkpoint

    section = config.section("document_similarity")
    if not section.get("enabled", True):
        return {"signatures": 0, "candidates": 0, "relationships": 0}
    from .scope import resolve_scope

    scope = resolve_scope(database, scope)
    entry_sql, entry_params = scope.entry_id_sql()
    marks = ",".join("?" for _ in _DOC_SUFFIXES)
    eligible = int(
        database.fetch_one(
            f"""SELECT COUNT(*) AS n FROM filesystem_entries e
                WHERE e.entry_type='file' AND e.id IN ({entry_sql})
                  AND lower(e.suffix) IN ({marks})""",
            (*entry_params, *_DOC_SUFFIXES),
        )["n"]
    )
    counts = {"signatures": 0, "candidates": 0, "relationships": 0}
    if maximum_documents is not None and eligible > maximum_documents:
        return {
            "status": "skipped",
            "reason": "quickstart_document_cost_gate",
            "documents": eligible,
            "maximum_documents": maximum_documents,
            **counts,
        }
    _ensure_document_objects(database, config, scope)
    num_perm = int(section["minhash_permutations"])
    shingle_size = int(section["shingle_size"])
    lsh_threshold = float(section["lsh_threshold"])
    verify_threshold = float(section["verification_threshold"])
    minimum_tokens = int(section.get("minimum_tokens", 20))
    fingerprint = _config_fingerprint(config)
    invalidate_content_relationships(database, ALGORITHM, ALGORITHM_VERSION, fingerprint)

    content_sql, content_params = scope.content_object_id_sql()

    signatures: dict[int, list[int]] = {}
    shingle_sets: dict[int, set[str]] = {}
    objects = database.fetch_all(
        f"""SELECT co.id FROM content_objects co WHERE co.id IN ({content_sql})
            AND EXISTS(SELECT 1 FROM entry_content_links l JOIN filesystem_entries e
                       ON e.id=l.entry_id WHERE l.content_object_id=co.id
                       AND lower(e.suffix) IN ({marks})) ORDER BY co.id""",
        (*content_params, *_DOC_SUFFIXES),
    )
    if maximum_documents is not None and len(objects) > maximum_documents:
        return {
            "status": "skipped",
            "reason": "quickstart_document_cost_gate",
            "documents": len(objects),
            "maximum_documents": maximum_documents,
            **counts,
        }
    total_tokens = 0
    for index, obj in enumerate(objects, start=1):
        cid = int(obj["id"])
        if job_id:
            check_cancelled(database, job_id)
        text = _document_text(database, config, cid)
        if text is None:
            continue
        tokens = tokenize(text[0])
        if len(tokens) < minimum_tokens:
            continue
        total_tokens += len(tokens)
        if maximum_tokens is not None and total_tokens > maximum_tokens:
            return {
                "status": "skipped",
                "reason": "quickstart_token_cost_gate",
                "documents": len(objects),
                "tokens": total_tokens,
                "maximum_tokens": maximum_tokens,
                **counts,
            }
        shingles = word_shingles(tokens, shingle_size)
        if not shingles:
            continue
        signature = minhash_signature(shingles, num_perm)
        signatures[cid] = signature
        shingle_sets[cid] = shingles
        checkpoint(database, job_id, processed_count=index)
    for cid, signature in signatures.items():
        database.connect().execute(
            """INSERT OR IGNORE INTO similarity_signatures(content_object_id,signature_type,signature_version,configuration_fingerprint,signature_blob,feature_count,status)
               VALUES(?,?,?,?,?,?, 'OK')""",
            (
                cid,
                "TEXT_MINHASH",
                SIGNATURE_VERSION,
                fingerprint,
                json.dumps(signature),
                len(shingle_sets[cid]),
            ),
        )
        counts["signatures"] += 1
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
