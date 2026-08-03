"""Persist chunks and occurrences; estimate and clear the chunk index safely."""

from __future__ import annotations

from .model import ChunkProfile, ChunkRecord

#: Bound-parameter windows. SQLite's default variable limit is 999 on older builds, so IN-lists and
#: value batches are split well under it.
_PARAM_WINDOW = 400


def _profile_object_bytes(conn, content_object_id: int, profile_id: int) -> int:
    """This object's current contribution to the profile's covered-byte total.

    An indexed lookup over one object's occurrences — the exact amount ``store_chunks`` is about to
    remove, so the caller's running index total can be adjusted by the net delta instead of
    re-aggregating the whole index per object.
    """
    row = conn.execute(
        "SELECT COALESCE(SUM(o.size_bytes),0) AS n FROM chunk_occurrences o "
        "JOIN content_chunks c ON c.id=o.chunk_id "
        "WHERE o.content_object_id=? AND c.chunking_profile_id=?",
        (content_object_id, profile_id),
    ).fetchone()
    return int(row["n"] or 0)


def store_chunks(
    database, content_object_id: int, profile_id: int, profile: ChunkProfile, records: list[ChunkRecord]
) -> int:
    """Store a content object's chunk sequence; return the net change in the profile's covered bytes.

    Idempotent per object: re-chunking the same content deletes its prior occurrences and re-inserts
    them, so the return value is ``new_bytes - deleted_bytes`` (zero for an unchanged re-chunk),
    which lets the caller keep a running index-size total without re-aggregating.

    Writes are batched — one ``INSERT`` of the distinct chunks, one of the occurrences, and one
    authoritative ``occurrence_count`` refresh over exactly the affected chunks — rather than four
    statements plus a correlated ``COUNT(*)`` subquery per chunk.
    """
    conn = database.connect()
    old_bytes = _profile_object_bytes(conn, content_object_id, profile_id)
    conn.execute("DELETE FROM chunk_occurrences WHERE content_object_id=?", (content_object_id,))
    if not records:
        conn.commit()
        return -old_bytes
    algorithm = profile.hash_algorithm
    # 1. Ensure every distinct chunk exists (one round trip; duplicates within the object collapse).
    conn.executemany(
        "INSERT OR IGNORE INTO content_chunks(chunking_profile_id,chunk_hash_algorithm,chunk_hash,size_bytes) VALUES(?,?,?,?)",
        [(profile_id, algorithm, r.chunk_hash, r.size_bytes) for r in records],
    )
    # 2. Resolve (chunk_hash, size) -> id for this object's distinct chunks, IN-list windowed.
    distinct_hashes = sorted({r.chunk_hash for r in records})
    ids: dict[tuple[str, int], int] = {}
    for start in range(0, len(distinct_hashes), _PARAM_WINDOW):
        hash_window = distinct_hashes[start : start + _PARAM_WINDOW]
        marks = ",".join("?" for _ in hash_window)
        for row in conn.execute(
            f"SELECT id,chunk_hash,size_bytes FROM content_chunks "
            f"WHERE chunking_profile_id=? AND chunk_hash_algorithm=? AND chunk_hash IN ({marks})",
            (profile_id, algorithm, *hash_window),
        ):
            ids[(row["chunk_hash"], int(row["size_bytes"]))] = int(row["id"])
    # 3. Insert this object's occurrences in one batch.
    conn.executemany(
        "INSERT OR REPLACE INTO chunk_occurrences(content_object_id,chunk_id,sequence_index,byte_offset,size_bytes) VALUES(?,?,?,?,?)",
        [
            (content_object_id, ids[(r.chunk_hash, r.size_bytes)], r.sequence_index, r.byte_offset, r.size_bytes)
            for r in records
        ],
    )
    # 4. Refresh occurrence_count once per affected chunk — the same authoritative COUNT the
    #    per-chunk UPDATE used, but batched instead of run for every chunk in the file.
    affected = sorted(set(ids.values()))
    for start in range(0, len(affected), _PARAM_WINDOW):
        id_window = affected[start : start + _PARAM_WINDOW]
        marks = ",".join("?" for _ in id_window)
        conn.execute(
            f"UPDATE content_chunks SET occurrence_count="
            f"(SELECT COUNT(*) FROM chunk_occurrences WHERE chunk_id=content_chunks.id) "
            f"WHERE id IN ({marks})",
            id_window,
        )
    conn.commit()
    new_bytes = sum(r.size_bytes for r in records)
    return new_bytes - old_bytes


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
