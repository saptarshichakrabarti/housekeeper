"""MinHash / LSH near-duplicate document tests (Tier-5, verified, review-only)."""

from housekeeper.analyzers.document_minhash import run_document_minhash_analysis
from housekeeper.scanner import DriveScanner
from housekeeper.similarity.lsh import candidate_pairs, choose_bands
from housekeeper.similarity.minhash import estimated_jaccard, minhash_signature
from housekeeper.similarity.shingling import exact_jaccard, tokenize, word_shingles


def test_shingling_and_exact_jaccard():
    tokens = tokenize("The quick brown fox jumps")
    shingles = word_shingles(tokens, 2)
    assert "the quick" in shingles
    assert exact_jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert exact_jaccard({"a"}, {"b"}) == 0.0


def test_minhash_estimate_tracks_true_jaccard():
    a = {f"shingle {i}" for i in range(100)}
    b = {f"shingle {i}" for i in range(20, 120)}  # 80% overlap
    sig_a = minhash_signature(a, 128)
    sig_b = minhash_signature(b, 128)
    estimate = estimated_jaccard(sig_a, sig_b)
    true = exact_jaccard(a, b)
    assert abs(estimate - true) < 0.12  # MinHash approximates Jaccard


def test_minhash_is_deterministic():
    shingles = {"one two three", "two three four"}
    assert minhash_signature(shingles, 64) == minhash_signature(shingles, 64)


def test_choose_bands_partitions_permutations():
    bands, rows = choose_bands(128, 0.75)
    assert bands * rows == 128


def test_lsh_candidates_only_for_similar():
    signatures = {
        1: minhash_signature({f"s {i}" for i in range(100)}, 64),
        2: minhash_signature({f"s {i}" for i in range(100)}, 64),  # identical
        3: minhash_signature({f"other {i}" for i in range(100)}, 64),
    }
    pairs = candidate_pairs(signatures, 64, 0.75)
    assert (1, 2) in pairs
    assert (1, 3) not in pairs


def test_near_duplicate_detected_and_verified(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    base = " ".join(f"the quick brown fox number {i} jumps over the lazy dog" for i in range(40))
    (root / "a.txt").write_text(base, encoding="utf-8")
    (root / "b.txt").write_text(base.replace("lazy dog", "sleepy cat", 1), encoding="utf-8")  # tiny edit
    (root / "unrelated.txt").write_text(
        " ".join(f"entirely distinct token {i} here" for i in range(40)), encoding="utf-8"
    )
    config.section("document_similarity")["minimum_tokens"] = 5
    DriveScanner(database, config).scan(root, incremental=False)
    result = run_document_minhash_analysis(database, config)
    assert result["signatures"] == 3
    rels = database.fetch_all("SELECT relationship_type,evidence_tier,confidence FROM content_relationships")
    assert len(rels) == 1
    assert rels[0]["evidence_tier"] == "TIER_5_PROBABILISTIC_SIMILARITY"
    assert rels[0]["relationship_type"] in {"NEAR_DUPLICATE_DOCUMENT", "TEXTUALLY_SIMILAR"}
    assert database.fetch_one("SELECT COUNT(*) n FROM exact_duplicate_groups")["n"] == 0


def test_unverified_candidate_produces_no_relationship(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    # Two documents that share boilerplate but differ substantially in body.
    boiler = "confidential header notice all rights reserved standard disclaimer boilerplate text " * 3
    (root / "a.txt").write_text(boiler + " ".join(f"alpha unique {i}" for i in range(60)), encoding="utf-8")
    (root / "b.txt").write_text(boiler + " ".join(f"beta different {i}" for i in range(60)), encoding="utf-8")
    config.section("document_similarity")["minimum_tokens"] = 5
    config.section("document_similarity")["verification_threshold"] = 0.9
    DriveScanner(database, config).scan(root, incremental=False)
    run_document_minhash_analysis(database, config)
    # Shared boilerplate must not merge them into a version family.
    assert database.fetch_all("SELECT * FROM content_relationships WHERE relationship_type='NEAR_DUPLICATE_DOCUMENT'") == []
