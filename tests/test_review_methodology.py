"""Review prioritization, lifecycle states, known-content registry, and event clustering."""

import time

import pytest

from housekeeper.analysers.exact_duplicates import run_exact_duplicate_analysis
from housekeeper.analysers.lifecycle import assign_state, run_lifecycle_analysis
from housekeeper.analysers.preservation_risk import run_preservation_risk_analysis
from housekeeper.analysers.review_priority import run_review_priority_analysis, score_entry
from housekeeper.constants import LifecycleState, ReviewPriorityCategory
from housekeeper.known_content import add_assertion, assertions_for_entry, list_assertions
from housekeeper.policies import classify_all_entries
from housekeeper.scanner import DriveScanner


class _Row(dict):
    def __getitem__(self, key):
        return super().get(key)


_WEIGHTS = {
    "recoverable_bytes": 1.0,
    "redundancy_confidence": 1.0,
    "regeneration_confidence": 1.0,
    "loss_risk": -2.0,
    "preservation_risk": -2.0,
    "review_effort": -0.5,
}


def test_priority_categorization():
    dup = _Row(classification="REVIEW_SAFE", primary_reason_code="EXACT_SHA256_DUPLICATE", size_bytes=1000)
    score, category, components = score_entry(dup, _WEIGHTS, has_preservation_risk=False)
    assert category == ReviewPriorityCategory.QUICK_SAFE_WIN
    assert components["redundancy_confidence"] == 1.0
    # Preservation risk always dominates -> PRESERVATION_FIRST.
    _, category2, _ = score_entry(dup, _WEIGHTS, has_preservation_risk=True)
    assert category2 == ReviewPriorityCategory.PRESERVATION_FIRST


def test_priority_stores_components(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.bin").write_bytes(b"dup")
    (root / "b.bin").write_bytes(b"dup")
    DriveScanner(database, config).scan(root, incremental=False)
    run_exact_duplicate_analysis(database, config)
    classify_all_entries(database, config)
    run_preservation_risk_analysis(database, config)
    counts = run_review_priority_analysis(database, config)
    assert counts.get("QUICK_SAFE_WIN", 0) >= 1
    row = database.fetch_one("SELECT components_json,explanation FROM review_priority LIMIT 1")
    assert "redundancy_confidence" in row["components_json"]
    assert row["explanation"]


def test_lifecycle_states():
    now = time.time()
    assert assign_state("PROTECTED", now, now)[0] == LifecycleState.PROTECTED
    assert assign_state("REVIEW_SAFE", now, now)[0] == LifecycleState.MANUAL_REVIEW
    assert assign_state("ERROR", now, now)[0] == LifecycleState.DEFERRED
    old = now - 3 * 365 * 24 * 3600
    assert assign_state("KEEP", old, now)[0] == LifecycleState.COLD_ARCHIVE
    assert assign_state("KEEP", now, now)[0] == LifecycleState.ARCHIVE


def test_lifecycle_analysis_over_tree(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "a.txt").write_text("x", encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    classify_all_entries(database, config)
    counts = run_lifecycle_analysis(database, config)
    assert sum(counts.values()) == 1
    assert database.fetch_one("SELECT state FROM entry_lifecycle")["state"] in {s.value for s in LifecycleState}


def test_known_content_registry(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "cache.tmp").write_text("regen", encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    add_assertion(database, "KNOWN_REGENERABLE", "PATH_PATTERN", "cache.tmp", {"why": "build cache"})
    assert len(list_assertions(database)) == 1
    entry = database.fetch_one("SELECT id FROM filesystem_entries WHERE name='cache.tmp'")["id"]
    assert "KNOWN_REGENERABLE" in assertions_for_entry(database, int(entry))


def test_known_content_rejects_unknown_assertion(database):
    with pytest.raises(ValueError):
        add_assertion(database, "NOT_A_REAL_ASSERTION", "PATH_PATTERN", "x")


def test_photo_event_clustering(config, database, tmp_path):
    from housekeeper.collections.events import run_photo_event_analysis

    root = tmp_path / "src"
    root.mkdir()
    # Three "photos" with close mtimes -> one event; a fourth far apart -> its own (dropped, <2).
    import os

    base = time.time()
    for i in range(3):
        p = root / f"img{i}.png"
        p.write_bytes(b"fake png")
        os.utime(p, (base + i * 60, base + i * 60))  # within 90-minute gap
    DriveScanner(database, config).scan(root, incremental=False)
    result = run_photo_event_analysis(database, config)
    assert result["photo_events"] == 1
    members = database.fetch_one(
        "SELECT COUNT(*) n FROM collection_members m JOIN collection_clusters c ON c.id=m.cluster_id WHERE c.cluster_type='PHOTO_EVENT'"
    )["n"]
    assert members == 3
