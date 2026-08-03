"""Content-defined chunking + partial-overlap tests (Tier-4)."""

import random

from housekeeper.analysers.content_defined_chunks import (
    run_chunk_analysis,
    run_chunk_overlap_analysis,
)
from housekeeper.chunking.index import clear_chunk_index, estimate_chunk_analysis
from housekeeper.chunking.model import ChunkProfile
from housekeeper.chunking.python_backend import chunk_file
from housekeeper.scanner import DriveScanner

_PROFILE = ChunkProfile("t", "fastcdc_gear", "1", 1024, 4096, 16384)


def _small_chunks(config):
    ch = config.section("chunking")
    ch["minimum_file_size_bytes"] = 1000
    ch["minimum_overlap_bytes"] = 20_000
    ch["profiles"]["balanced"] = {
        "minimum_chunk_size": 1024,
        "average_chunk_size": 4096,
        "maximum_chunk_size": 16384,
    }


def test_chunker_is_deterministic_and_covers_file(tmp_path):
    data = bytes(random.Random(1).getrandbits(8) for _ in range(50_000))
    path = tmp_path / "f.bin"
    path.write_bytes(data)
    first = list(chunk_file(path, _PROFILE))
    second = list(chunk_file(path, _PROFILE))
    assert [c.chunk_hash for c in first] == [c.chunk_hash for c in second]
    assert sum(c.size_bytes for c in first) == len(data)  # full coverage, no gaps
    assert first[0].byte_offset == 0


def test_insertion_resilient_overlap(config, database, tmp_path):
    _small_chunks(config)
    root = tmp_path / "src"
    root.mkdir()
    rng = random.Random(7)
    body = bytes(rng.getrandbits(8) for _ in range(200_000))
    (root / "a.bin").write_bytes(body)
    (root / "b.bin").write_bytes(bytes(rng.getrandbits(8) for _ in range(8000)) + body)  # prefix insert
    (root / "unrelated.bin").write_bytes(bytes(rng.getrandbits(8) for _ in range(150_000)))
    DriveScanner(database, config).scan(root, incremental=False)
    run_chunk_analysis(database, config)
    run_chunk_overlap_analysis(database, config)
    rels = database.fetch_all(
        "SELECT relationship_type,evidence_tier,confidence FROM content_relationships"
    )
    assert len(rels) == 1  # only a<->b
    assert rels[0]["evidence_tier"] == "TIER_4_PARTIAL_OVERLAP"
    assert rels[0]["confidence"] >= 0.9  # near-subset despite the insertion
    # A partial-overlap match is never an exact-duplicate group.
    assert database.fetch_one("SELECT COUNT(*) n FROM exact_duplicate_groups")["n"] == 0


def test_occurrence_counts_and_index_bytes_are_exact_and_stable(config, database, tmp_path):
    """Batched writes must keep occurrence_count authoritative and the covered-byte total drift-free.

    Two files share a common body, so at least one chunk occurs more than once. The recount runs
    once per affected chunk now, not per chunk in the file, so this asserts it still equals the true
    row count — and that re-running the (idempotent) stage neither doubles counts nor grows the index.
    """
    _small_chunks(config)
    root = tmp_path / "src"
    root.mkdir()
    rng = random.Random(11)
    shared = bytes(rng.getrandbits(8) for _ in range(120_000))
    (root / "a.bin").write_bytes(shared)
    (root / "b.bin").write_bytes(bytes(rng.getrandbits(8) for _ in range(6000)) + shared)
    DriveScanner(database, config).scan(root, incremental=False)
    run_chunk_analysis(database, config)

    def occurrence_snapshot():
        return {
            int(r["id"]): int(r["occurrence_count"])
            for r in database.fetch_all("SELECT id,occurrence_count FROM content_chunks")
        }

    def true_counts():
        return {
            int(r["chunk_id"]): int(r["n"])
            for r in database.fetch_all(
                "SELECT chunk_id,COUNT(*) n FROM chunk_occurrences GROUP BY chunk_id"
            )
        }

    stored = occurrence_snapshot()
    assert stored == true_counts()  # occurrence_count equals the real number of occurrences
    assert max(stored.values()) >= 2  # the shared body really does produce a repeated chunk

    # The covered-byte identity the index-full gate relies on: SUM(size*count) == SUM(occurrence bytes).
    weighted = database.fetch_one(
        "SELECT COALESCE(SUM(size_bytes*occurrence_count),0) n FROM content_chunks"
    )["n"]
    occ_bytes = database.fetch_one(
        "SELECT COALESCE(SUM(size_bytes),0) n FROM chunk_occurrences"
    )["n"]
    assert weighted == occ_bytes

    # Idempotent re-run: identical counts, no doubling, no phantom index growth.
    run_chunk_analysis(database, config)
    assert occurrence_snapshot() == stored
    assert occurrence_snapshot() == true_counts()


def test_estimate_and_clear_are_derived_only(config, database, tmp_path):
    _small_chunks(config)
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.bin").write_bytes(bytes(random.Random(3).getrandbits(8) for _ in range(60_000)))
    DriveScanner(database, config).scan(root, incremental=False)
    run_chunk_analysis(database, config)
    estimate = estimate_chunk_analysis(database, config)
    assert estimate["candidate_content_objects"] >= 1
    chunks_before = database.fetch_one("SELECT COUNT(*) n FROM content_chunks")["n"]
    assert chunks_before > 0
    clear_chunk_index(database, None, dry_run=True)  # dry run removes nothing
    assert database.fetch_one("SELECT COUNT(*) n FROM content_chunks")["n"] == chunks_before
    clear_chunk_index(database, None, dry_run=False)
    assert database.fetch_one("SELECT COUNT(*) n FROM content_chunks")["n"] == 0
    # Source files and content objects are untouched by clearing derived data.
    assert (root / "a.bin").exists()
    assert database.fetch_one("SELECT COUNT(*) n FROM content_objects")["n"] >= 1
