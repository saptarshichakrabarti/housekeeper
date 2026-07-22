"""Benchmark baseline recording + relative-regression comparison.

Timings are machine-dependent, so these tests never assert an absolute wall-clock number. They
assert (a) that counts are deterministic and reproducible, (b) that a count drift always fails a
comparison, (c) that a timing regression fails only on the same runner, and (d) that the committed
`benchmarks/baseline.json` still matches a fresh run's counts — the actual regression guard.
"""

import copy
import json
from pathlib import Path

from housekeeper import benchmarking

TINY = {"tiny": (8, 2)}


def test_counts_are_deterministic_and_reproducible(tmp_path):
    first = benchmarking.run_suite(tmp_path / "a", TINY)
    second = benchmarking.run_suite(tmp_path / "b", TINY)
    # Same corpus shape => identical entity counts on every run and platform.
    assert first["profiles"]["tiny"]["counts"] == second["profiles"]["tiny"]["counts"]
    counts = first["profiles"]["tiny"]["counts"]
    assert counts["files"] == 8
    assert counts["directories"] == 2
    # i in {0,5} share one payload => 6 unique + 1 shared = 7 content objects, 1 duplicate group.
    assert counts["content_objects"] == 7
    assert counts["exact_duplicate_groups"] == 1


def test_compare_passes_against_matching_baseline(tmp_path):
    baseline = benchmarking.run_suite(tmp_path / "base", TINY)
    current = benchmarking.run_suite(tmp_path / "cur", TINY)
    result = benchmarking.compare(current, baseline)
    assert result["ok"] is True
    assert result["same_environment"] is True
    assert result["count_regressions"] == []
    assert result["timing_regressions"] == []


def test_count_regression_always_fails(tmp_path):
    baseline = benchmarking.run_suite(tmp_path / "base", TINY)
    current = copy.deepcopy(baseline)
    # Simulate a correctness drift: the run now produces one fewer content object.
    current["profiles"]["tiny"]["counts"]["content_objects"] -= 1
    result = benchmarking.compare(current, baseline)
    assert result["ok"] is False
    assert any(r.get("metric") == "content_objects" for r in result["count_regressions"])


def test_timing_regression_fails_only_on_same_runner(tmp_path):
    baseline = benchmarking.run_suite(tmp_path / "base", TINY)
    current = copy.deepcopy(baseline)
    # Force the current run to look far slower than the recorded baseline.
    baseline["profiles"]["tiny"]["seconds"] = 0.001
    current["profiles"]["tiny"]["seconds"] = 1.0

    same_runner = benchmarking.compare(current, baseline, timing_tolerance=0.5)
    assert same_runner["ok"] is False
    assert same_runner["timing_regressions"]

    # A different runner cannot be compared on wall-clock; the timing check is skipped, not failed.
    current["environment"] = {**current["environment"], "machine": "other-cpu"}
    different_runner = benchmarking.compare(current, baseline, timing_tolerance=0.5)
    assert different_runner["same_environment"] is False
    assert different_runner["timing_regressions"] == []
    assert "tiny" in different_runner["timing_skipped"]
    assert different_runner["ok"] is True  # counts still match


def test_missing_profile_is_a_regression(tmp_path):
    baseline = benchmarking.run_suite(tmp_path / "base", TINY)
    current = benchmarking.run_suite(tmp_path / "cur", TINY)
    baseline["profiles"] = {}  # baseline lacks the profile the current run produced
    result = benchmarking.compare(current, baseline)
    assert result["ok"] is False
    assert any(r.get("reason") == "absent from baseline" for r in result["count_regressions"])


def test_committed_baseline_matches_fresh_run(tmp_path):
    baseline_path = Path(benchmarking.__file__).resolve().parents[2] / "benchmarks" / "baseline.json"
    assert baseline_path.exists(), "committed benchmarks/baseline.json is missing"
    baseline = json.loads(baseline_path.read_text(encoding="utf-8"))
    assert baseline["schema_version"] == benchmarking.BASELINE_SCHEMA_VERSION
    # The real regression guard: a fresh run of the committed profiles must reproduce the recorded
    # counts exactly (timing is not asserted here, since CI may run on a different machine).
    current = benchmarking.run_suite(tmp_path / "fresh", benchmarking.PROFILES)
    for name, recorded in baseline["profiles"].items():
        assert current["profiles"][name]["counts"] == recorded["counts"], name
