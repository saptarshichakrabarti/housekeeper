"""Persist chunks and occurrences; estimate and clear the chunk index safely."""

from __future__ import annotations

from .model import ChunkProfile, ChunkRecord


def store_chunks(
    database, content_object_id: int, profile_id: int, profile: ChunkProfile, records: list[ChunkRecord]
) -> int:
    """Store a content object's chunk sequence; return total chunk bytes. Idempotent per object."""
    conn = database.connect()
    conn.execute("DELETE FROM chunk_occurrences WHERE content_object_id=?", (content_object_id,))
    total = 0
    for record in records:
        conn.execute(
            "INSERT OR IGNORE INTO content_chunks(chunking_profile_id,chunk_hash_algorithm,chunk_hash,size_bytes) VALUES(?,?,?,?)",
            (profile_id, profile.hash_algorithm, record.chunk_hash, record.size_bytes),
        )
        chunk_row = conn.execute(
            "SELECT id FROM content_chunks WHERE chunking_profile_id=? AND chunk_hash_algorithm=? AND chunk_hash=? AND size_bytes=?",
            (profile_id, profile.hash_algorithm, record.chunk_hash, record.size_bytes),
        ).fetchone()
        chunk_id = int(chunk_row["id"])
        conn.execute(
            "INSERT OR REPLACE INTO chunk_occurrences(content_object_id,chunk_id,sequence_index,byte_offset,size_bytes) VALUES(?,?,?,?,?)",
            (content_object_id, chunk_id, record.sequence_index, record.byte_offset, record.size_bytes),
        )
        conn.execute(
            "UPDATE content_chunks SET occurrence_count=(SELECT COUNT(*) FROM chunk_occurrences WHERE chunk_id=?) WHERE id=?",
            (chunk_id, chunk_id),
        )
        total += record.size_bytes
    conn.commit()
    return total


def estimate_chunk_analysis(database, config, profile_id: int | None = None) -> dict[str, int]:
    """Report the cost of chunking eligible content objects before doing the work."""
    minimum = int(config.section("chunking")["minimum_file_size_bytes"])
    average = int(config.section("chunking")["profiles"][config.section("chunking")["default_profile"]]["average_chunk_size"])
    row = database.fetch_one(
        "SELECT COUNT(*) AS n, COALESCE(SUM(size_bytes),0) AS bytes FROM content_objects WHERE size_bytes>=?",
        (minimum,),
    )
    candidate_bytes = int(row["bytes"])
    return {
        "candidate_content_objects": int(row["n"]),
        "candidate_bytes": candidate_bytes,
        "expected_chunks": candidate_bytes // max(1, average),
        # ~ 32-byte hash + small row overhead per chunk.
        "estimated_index_bytes": (candidate_bytes // max(1, average)) * 96,
    }


def clear_chunk_index(database, profile_id: int | None = None, dry_run: bool = True) -> dict[str, int]:
    """Remove derived chunk data only. Never touches source files, decisions, or raw hashes."""
    where = "WHERE chunking_profile_id=?" if profile_id is not None else ""
    params: tuple = (profile_id,) if profile_id is not None else ()
    occ = database.fetch_one(
        f"SELECT COUNT(*) n FROM chunk_occurrences WHERE chunk_id IN (SELECT id FROM content_chunks {where})",
        params,
    )["n"]
    chunks = database.fetch_one(f"SELECT COUNT(*) n FROM content_chunks {where}", params)["n"]
    result = {"chunk_occurrences": int(occ), "content_chunks": int(chunks), "dry_run": int(dry_run)}
    if not dry_run:
        conn = database.connect()
        conn.execute(
            f"DELETE FROM chunk_occurrences WHERE chunk_id IN (SELECT id FROM content_chunks {where})",
            params,
        )
        conn.execute(f"DELETE FROM content_chunks {where}", params)
        conn.execute(
            "DELETE FROM content_overlap_results" + (" WHERE chunking_profile_id=?" if profile_id is not None else ""),
            params,
        )
        conn.commit()
    return result
