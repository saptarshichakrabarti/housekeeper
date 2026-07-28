"""Benchmark baseline recording and relative-regression comparison.

Wall-clock numbers are not portable across machines, so a committed baseline separates two kinds
of measurement:

* **Counts** (files, directories, content objects, duplicate groups) are deterministic and
  machine-independent. Any drift between a run and the baseline is a *correctness* regression and
  always fails a comparison.
* **Timings** are machine-dependent. They are compared only when the current environment
  fingerprint matches the one recorded in the baseline (per ``benchmarks/README.md``: "compare
  against an explicitly recorded baseline on the same runner ... fail only on an agreed relative
  regression, not a fixed wall-clock value"). On a different runner, timing checks are reported as
  skipped rather than failed.

The corpus is generated deterministically here (not via the richer synthetic fixture, whose shape
may evolve) so the recorded counts are stable across time and platform.
"""

from __future__ import annotations

import platform
import time
from pathlib import Path

BASELINE_SCHEMA_VERSION = 1

# Below this, wall clock is scheduler noise rather than signal: a 0.2 s profile routinely varies by
# more than any relative tolerance worth setting, so comparing it fails runs at random and teaches
# people to ignore the benchmark. Such profiles are reported as timing-skipped; their counts, which
# are deterministic, are still compared. `xlarge` exists to be above this floor.
MINIMUM_TIMED_SECONDS = 1.0

# name -> (file_count, dir_count). Kept modest so recording a baseline and the regression test stay
# fast; the shapes still exercise traversal, hashing, exact-duplicate grouping, and directory
# overlap at three scales.
PROFILES: dict[str, tuple[int, int]] = {
    "small": (12, 3),
    "medium": (60, 6),
    "large": (240, 12),
    # At 240 files every plan in this codebase looks fine. `xlarge` is the smallest shape that
    # makes per-entry work visible; keep the largest profile well above the point where a full
    # table scan is indistinguishable from an index seek.
    "xlarge": (2_000, 24),
}


def environment_fingerprint() -> dict[str, str]:
    """The machine attributes that make wall-clock timings comparable.

    Deliberately excludes the git commit: counts must hold across commits, and timings are gated on
    the *runner*, not the revision. The commit is recorded separately as informational provenance.
    """
    return {
        "python": platform.python_version(),
        "implementation": platform.python_implementation(),
        "system": platform.system(),
        "machine": platform.machine(),
    }


def _git_commit() -> str:
    import subprocess

    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,  # a repo without git, or a detached checkout, is not an error here
        )
        return out.stdout.strip() if out.returncode == 0 else "unknown"
    except (OSError, subprocess.SubprocessError):
        return "unknown"


def _build_corpus(root: Path, file_count: int, dir_count: int) -> None:
    """Create a deterministic corpus: files spread across directories, every fifth file sharing
    one identical payload so exact-duplicate grouping has something to find. Same inputs always
    produce the same entity counts on any platform."""
    root.mkdir(parents=True, exist_ok=True)
    directories = []
    for index in range(dir_count):
        directory = root / f"dir_{index:03d}"
        directory.mkdir(exist_ok=True)
        directories.append(directory)
    for index in range(file_count):
        target = directories[index % dir_count]
        payload = (
            b"benchmark-shared-content"
            if index % 5 == 0
            else f"benchmark-unique-file-{index}".encode()
        )
        (target / f"file_{index:04d}.txt").write_bytes(payload)


def run_profile(workspace_root: Path, name: str, file_count: int, dir_count: int) -> dict:
    """Build one profile's corpus, run scan + exact-duplicate + directory-overlap, and return its
    deterministic counts alongside the wall-clock time on this runner."""
    from .analysers.directory_overlap import run_directory_overlap_analysis
    from .analysers.exact_duplicates import run_exact_duplicate_analysis
    from .config import load_config
    from .database import Database
    from .scanner import DriveScanner

    corpus = workspace_root / f"corpus_{name}"
    _build_corpus(corpus, file_count, dir_count)
    config = load_config(workspace_override=workspace_root / f"ws_{name}")
    # Ensure even the tiny profiles produce overlap candidates deterministically.
    config.section("directory_overlap")["minimum_files"] = 1
    config.section("directory_overlap")["minimum_bytes"] = 0
    database = Database(config.database_path)
    database.initialize()
    try:
        start = time.perf_counter()
        DriveScanner(database, config).scan(corpus, incremental=False)
        run_exact_duplicate_analysis(database, config)
        run_directory_overlap_analysis(database, config)
        seconds = time.perf_counter() - start

        def count(sql: str) -> int:
            row = database.fetch_one(sql)
            return int(row["n"]) if row else 0

        counts = {
            "files": count("SELECT COUNT(*) n FROM filesystem_entries WHERE entry_type='file'"),
            "directories": count(
                "SELECT COUNT(*) n FROM filesystem_entries WHERE entry_type='directory'"
            ),
            "content_objects": count("SELECT COUNT(*) n FROM content_objects"),
            "exact_duplicate_groups": count("SELECT COUNT(*) n FROM exact_duplicate_groups"),
        }
    finally:
        database.close()
    return {
        "profile": name,
        "file_count": file_count,
        "dir_count": dir_count,
        "counts": counts,
        "seconds": round(seconds, 4),
    }


def run_suite(workspace_root: Path, profiles: dict[str, tuple[int, int]] | None = None) -> dict:
    selected = profiles or PROFILES
    results = {
        name: run_profile(workspace_root, name, file_count, dir_count)
        for name, (file_count, dir_count) in selected.items()
    }
    return {
        "schema_version": BASELINE_SCHEMA_VERSION,
        "environment": environment_fingerprint(),
        "commit": _git_commit(),
        "profiles": results,
    }


def compare(current: dict, baseline: dict, timing_tolerance: float = 0.5) -> dict:
    """Diff a fresh suite result against a recorded baseline.

    Count drift always fails. Timing regression (> ``timing_tolerance`` fraction slower) fails only
    when the runner matches the baseline's recorded environment; otherwise it is reported skipped.
    """
    same_environment = current.get("environment") == baseline.get("environment")
    count_regressions: list[dict] = []
    timing_regressions: list[dict] = []
    timing_skipped: list[str] = []
    for name, current_profile in current["profiles"].items():
        base_profile = baseline["profiles"].get(name)
        if base_profile is None:
            count_regressions.append({"profile": name, "reason": "absent from baseline"})
            continue
        for metric, current_value in current_profile["counts"].items():
            baseline_value = base_profile["counts"].get(metric)
            if baseline_value != current_value:
                count_regressions.append(
                    {
                        "profile": name,
                        "metric": metric,
                        "baseline": baseline_value,
                        "current": current_value,
                    }
                )
        baseline_seconds = base_profile["seconds"]
        if not same_environment or baseline_seconds < MINIMUM_TIMED_SECONDS:
            timing_skipped.append(name)
            continue
        limit = baseline_seconds * (1 + timing_tolerance)
        if current_profile["seconds"] > limit:
            timing_regressions.append(
                {
                    "profile": name,
                    "baseline_seconds": baseline_seconds,
                    "current_seconds": current_profile["seconds"],
                    "limit_seconds": round(limit, 4),
                }
            )
    return {
        "ok": not count_regressions and not timing_regressions,
        "same_environment": same_environment,
        "count_regressions": count_regressions,
        "timing_regressions": timing_regressions,
        "timing_skipped": timing_skipped,
    }


def default_baseline_path() -> Path:
    return Path(__file__).resolve().parents[2] / "benchmarks" / "baseline.json"


def write_baseline(path: Path, suite: dict) -> None:
    import json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(suite, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_baseline(path: Path) -> dict:
    import json

    return json.loads(path.read_text(encoding="utf-8"))
