"""Directory-overlap tests: pure containment/jaccard maths and integration containment."""

from housekeeper.analyzers.directory_overlap import (
    calculate_containment,
    calculate_jaccard,
    generate_candidate_directory_pairs,
    run_directory_overlap_analysis,
)
from housekeeper.analyzers.exact_duplicates import run_exact_duplicate_analysis
from housekeeper.scanner import DriveScanner


def test_calculate_containment():
    assert calculate_containment({"a", "b"}, {"a", "b", "c"}) == 1.0
    assert calculate_containment({"a", "b"}, {"a"}) == 0.5
    assert calculate_containment(set(), {"a"}) == 0.0


def test_calculate_jaccard():
    assert calculate_jaccard({"a", "b"}, {"a", "b"}) == 1.0
    assert calculate_jaccard({"a"}, {"b"}) == 0.0


def test_candidate_pairs_only_for_shared_content():
    dir_hashes = {
        1: {"x", "y"},
        2: {"x", "z"},  # shares 'x' with dir 1
        3: {"q"},  # shares nothing -> never a candidate
    }
    pairs = generate_candidate_directory_pairs(dir_hashes)
    assert pairs == {(1, 2)}


def test_candidate_generation_skips_unrelated_directories():
    dir_hashes = {i: {f"unique-{i}"} for i in range(50)}
    assert generate_candidate_directory_pairs(dir_hashes) == set()


def test_full_containment_detected(config, database, tmp_path):
    root = tmp_path / "src"
    older = root / "old"
    newer = root / "new"
    older.mkdir(parents=True)
    newer.mkdir(parents=True)
    for base in (older, newer):
        (base / "a.txt").write_text("content a", encoding="utf-8")
        (base / "b.txt").write_text("content b", encoding="utf-8")
    (newer / "c.txt").write_text("only in newer", encoding="utf-8")
    config.section("directory_overlap")["minimum_files"] = 1
    config.section("directory_overlap")["minimum_bytes"] = 0
    DriveScanner(database, config).scan(root, incremental=False)
    run_exact_duplicate_analysis(database, config)  # ensure full hashes exist
    run_directory_overlap_analysis(database, config)
    overlaps = database.fetch_all(
        "SELECT * FROM relationships WHERE relationship_type='MOSTLY_CONTAINED_IN'"
    )
    assert overlaps  # 'old' is fully contained in 'new'
