"""Cross-drive coverage: which of a source's files are verified present on another source.

The safety-critical assertion is the third bucket. A file with no verified hash must never be
counted as covered — "we did not look" is not "it exists elsewhere". The other trap is history: a
copy that only exists in an *older* snapshot is not a copy that exists now.
"""

from __future__ import annotations

from housekeeper.analysers.exact_duplicates import run_exact_duplicate_analysis
from housekeeper.coverage import coverage, source_roots
from housekeeper.reports.generator import generate_report
from housekeeper.scanner import DriveScanner


def _two_drives(config, database, tmp_path):
    """Drive A and drive B share two files; each has one of its own."""
    a, b = tmp_path / "drive-a", tmp_path / "drive-b"
    a.mkdir()
    b.mkdir()
    for name in ("shared-one.txt", "shared-two.txt"):
        (a / name).write_text(f"content of {name}", encoding="utf-8")
        (b / name).write_text(f"content of {name}", encoding="utf-8")
    (a / "only-on-a.txt").write_text("a" * 4096, encoding="utf-8")
    (b / "only-on-b.txt").write_text("b" * 4096, encoding="utf-8")
    scanner = DriveScanner(database, config)
    scanner.scan(a, incremental=False)
    scanner.scan(b, incremental=False)
    run_exact_duplicate_analysis(database, config)  # establishes verified content identity
    ids = {source["name"]: source["id"] for source in source_roots(database)}
    return ids


def test_buckets_split_covered_unique_and_unknown(config, database, tmp_path):
    ids = _two_drives(config, database, tmp_path)
    result = coverage(database, ids["drive-a"])
    assert result["buckets"]["covered"]["count"] == 2
    assert result["buckets"]["unique"]["count"] == 1
    assert result["buckets"]["unknown"]["count"] == 0
    assert result["total_files"] == 3
    assert [f["relative_path"] for f in result["unique_files"]] == ["only-on-a.txt"]
    assert "2 of 3 files verified elsewhere" in result["summary"]


def test_a_file_without_a_verified_hash_is_unknown_not_covered(config, database, tmp_path):
    ids = _two_drives(config, database, tmp_path)
    # Drop the identity of one shared file on drive A: nothing is known about it any more, and
    # "unknown" is the only honest bucket — even though its twin is still on drive B.
    database.connect().execute(
        """DELETE FROM entry_content_links WHERE entry_id IN (
             SELECT e.id FROM current_entries e WHERE e.source_root_id=? AND e.name='shared-one.txt')""",
        (ids["drive-a"],),
    )
    database.connect().commit()
    result = coverage(database, ids["drive-a"])
    assert result["buckets"]["unknown"]["count"] == 1
    assert result["buckets"]["covered"]["count"] == 1


def test_a_copy_that_only_exists_in_an_older_snapshot_is_not_coverage(config, database, tmp_path):
    ids = _two_drives(config, database, tmp_path)
    b_root = tmp_path / "drive-b"
    (b_root / "shared-one.txt").unlink()
    (b_root / "shared-two.txt").unlink()
    DriveScanner(database, config).scan(b_root, incremental=True)
    # Drive B's old snapshot still contains both files; its *current* one does not, so drive A is
    # no longer covered by it.
    result = coverage(database, ids["drive-a"])
    assert result["buckets"]["covered"]["count"] == 0
    assert result["buckets"]["unique"]["count"] == 3


def test_against_restricts_the_comparison(config, database, tmp_path):
    ids = _two_drives(config, database, tmp_path)
    against_self = coverage(database, ids["drive-a"], against=[ids["drive-a"]])
    # A source can never cover itself: the join excludes its own entries by source, so restricting
    # the comparison to itself leaves nothing covered.
    assert against_self["buckets"]["covered"]["count"] == 0
    assert against_self["total_files"] == 3
    filtered = coverage(database, ids["drive-a"], against=[ids["drive-b"]])
    assert filtered["buckets"]["covered"]["count"] == 2
    # The file list must come from the requested source, not from the one being compared against —
    # the two queries bind the `against` ids at different positions, so they are bound by name.
    assert [f["relative_path"] for f in filtered["unique_files"]] == ["only-on-a.txt"]
    assert {f["relative_path"] for f in against_self["unique_files"]} == {
        "only-on-a.txt",
        "shared-one.txt",
        "shared-two.txt",
    }


def test_a_file_nothing_ever_hashed_is_unknown(config, database, tmp_path):
    """A single file of its size is never hashed (identity is candidate-driven), so it is unknown.

    Not "unique": uniqueness is a claim about content, and without a hash there is no content
    identity to claim it with. Unknown is the bucket that says "we did not look".
    """
    root = tmp_path / "lonely"
    root.mkdir()
    (root / "file.txt").write_text("only copy anywhere", encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    run_exact_duplicate_analysis(database, config)
    only = source_roots(database)[0]["id"]
    result = coverage(database, only)
    assert result["buckets"]["covered"]["count"] == 0
    assert result["buckets"]["unknown"]["count"] == 1
    assert result["buckets"]["unique"]["count"] == 0


def test_coverage_report_never_says_safe_to_delete(config, database, tmp_path):
    _two_drives(config, database, tmp_path)
    html = generate_report("coverage", database, config).read_text(encoding="utf-8")
    assert "verified elsewhere" in html
    assert "only copy here" in html
    # The phrase appears only inside its own denial, never as a claim about a file.
    assert "not</b> that anything is safe to delete" in html
    assert html.lower().count("safe to delete") == 1
    assert "drive-a" in html and "drive-b" in html


def test_dashboard_coverage_page_renders_buckets(config, database, tmp_path):
    import pytest

    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from housekeeper.dashboard.app import create_app

    _two_drives(config, database, tmp_path)
    client = TestClient(create_app(database, config=config))
    html = client.get("/coverage").text
    assert "Verified elsewhere" in html and "Only copy here" in html
    assert "Unknown (no verified hash)" in html
    assert "not</strong> say anything is safe to delete" in html
    # Reachable from the navigation, not just by URL.
    assert 'href="/coverage"' in client.get("/").text
