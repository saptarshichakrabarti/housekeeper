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
