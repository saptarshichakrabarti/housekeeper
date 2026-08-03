"""Binary fuzzy-similarity (TLSH) analyser: capability gating, parallel digestion, determinism."""

import random

import pytest

from housekeeper.analysers.binary_similarity import run_binary_similarity_analysis
from housekeeper.analysers.registry import run_content_analysis
from housekeeper.scanner import DriveScanner
from housekeeper.similarity.fuzzy_hashes import capabilities

pytestmark = pytest.mark.skipif(
    not capabilities()["TLSH_AVAILABLE"], reason="tlsh backend not installed"
)


def _tlsh_friendly(seed: int, length: int = 20_000) -> bytes:
    """Non-uniform, text-like bytes: TLSH rejects flat-random data as too low-complexity."""
    rng = random.Random(seed)
    alphabet = bytes(range(32, 127))
    return bytes(alphabet[rng.randrange(len(alphabet))] for _ in range(length))


def _make_corpus(root):
    root.mkdir()
    base = _tlsh_friendly(1)
    near = bytearray(base)
    for i in range(0, 400, 9):  # a light perturbation stays within the TLSH threshold
        near[i] = 32 + (near[i] + 3) % 90
    (root / "base.bin").write_bytes(base)
    (root / "near.bin").write_bytes(bytes(near))
    (root / "unrelated.bin").write_bytes(_tlsh_friendly(777))


def _enable(config):
    config.section("binary_similarity")["tlsh_enabled"] = True
    config.section("binary_similarity")["minimum_file_size_bytes"] = 512


def test_tlsh_finds_similar_pair_only(config, database, tmp_path):
    _enable(config)
    root = tmp_path / "src"
    _make_corpus(root)
    DriveScanner(database, config).scan(root, incremental=False)
    run_content_analysis(database, config, None)  # establish content objects to digest
    result = run_binary_similarity_analysis(database, config)
    assert result["status"] == "ok"
    rels = database.fetch_all(
        "SELECT relationship_type,evidence_tier,confidence FROM content_relationships"
    )
    assert len(rels) == 1  # only base <-> near; the unrelated file shares no similarity
    assert rels[0]["evidence_tier"] == "TIER_5_PROBABILISTIC_SIMILARITY"
    assert rels[0]["relationship_type"] == "SEMANTICALLY_SIMILAR"


def test_tlsh_output_is_deterministic_under_parallel_digestion(config, database, tmp_path):
    """Digests complete in pool order, but the stored pairs must not depend on that order."""
    _enable(config)
    # Force multiple workers so completion order genuinely varies between runs.
    config.section("performance").setdefault("overrides", {})["full_hash_workers"] = 4
    root = tmp_path / "src"
    _make_corpus(root)
    DriveScanner(database, config).scan(root, incremental=False)
    run_content_analysis(database, config, None)

    def run_and_snapshot():
        database.connect().execute("DELETE FROM content_relationships")
        database.connect().commit()
        run_binary_similarity_analysis(database, config)
        return database.fetch_all(
            "SELECT source_id,target_id,relationship_type,confidence "
            "FROM content_relationships ORDER BY source_id,target_id"
        )

    first = [tuple(r) for r in run_and_snapshot()]
    second = [tuple(r) for r in run_and_snapshot()]
    assert first == second
    assert len(first) == 1


def test_tlsh_disabled_reports_capability_without_fabricating(config, database, tmp_path):
    root = tmp_path / "src"
    _make_corpus(root)
    DriveScanner(database, config).scan(root, incremental=False)
    run_content_analysis(database, config, None)
    # tlsh_enabled defaults to False: the stage must no-op honestly, not invent relationships.
    result = run_binary_similarity_analysis(database, config)
    assert result["status"] == "unavailable"
    assert result["relationships"] == 0
    assert database.fetch_one("SELECT COUNT(*) n FROM content_relationships")["n"] == 0
