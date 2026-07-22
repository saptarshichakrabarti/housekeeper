"""Cooperative pause/cancel is now honored by every analyzer, including ones that previously ran
to completion regardless of a control request.

The ``checkpoint`` helper is the single cooperative point: it honors a pending pause/cancel and
records progress, and is a no-op without a job. These tests exercise the helper directly and prove
that a representative directory-overlap job and a collections job (which had no ``job_id`` at all
before this change) both stop at their next checkpoint when paused or cancelled.
"""

import pytest

from housekeeper.analyzers.directory_overlap import run_directory_overlap_analysis
from housekeeper.analyzers.exact_duplicates import run_exact_duplicate_analysis
from housekeeper.collections.events import run_acquisition_batch_analysis
from housekeeper.jobs import (
    JobCancelled,
    JobPaused,
    checkpoint,
    create_job,
    request_cancel,
    request_pause,
    update_job,
)
from housekeeper.scanner import DriveScanner


def _status(database, job_id):
    return database.fetch_one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"]


def test_checkpoint_is_noop_without_job(database):
    # No job -> no error and nothing recorded; analyzers stay directly callable outside a job.
    checkpoint(database, None, processed_count=5)


def test_checkpoint_honors_pause(database):
    job_id = create_job(database, "TEST")
    update_job(database, job_id, "RUNNING")
    request_pause(database, job_id)
    with pytest.raises(JobPaused):
        checkpoint(database, job_id, processed_count=1)
    assert _status(database, job_id) == "PAUSED"


def test_checkpoint_honors_cancel(database):
    job_id = create_job(database, "TEST")
    update_job(database, job_id, "RUNNING")
    request_cancel(database, job_id)
    with pytest.raises(JobCancelled):
        checkpoint(database, job_id, processed_count=1)
    assert _status(database, job_id) == "CANCELLED"


def _two_overlapping_dirs(config, database, tmp_path):
    root = tmp_path / "src"
    a = root / "A"
    b = root / "B"
    a.mkdir(parents=True)
    b.mkdir(parents=True)
    for name in ("one.txt", "two.txt", "three.txt"):
        (a / name).write_text(f"shared {name}", encoding="utf-8")
        (b / name).write_text(f"shared {name}", encoding="utf-8")
    config.section("directory_overlap")["minimum_files"] = 1
    config.section("directory_overlap")["minimum_bytes"] = 0
    DriveScanner(database, config).scan(root, incremental=False)
    run_exact_duplicate_analysis(database, config)  # hash so candidate pairs exist


def test_directory_overlap_honors_pause(config, database, tmp_path):
    _two_overlapping_dirs(config, database, tmp_path)
    job_id = create_job(database, "DIRECTORY_OVERLAP")
    update_job(database, job_id, "RUNNING")
    request_pause(database, job_id)
    with pytest.raises(JobPaused):
        run_directory_overlap_analysis(database, config, None, job_id)
    assert _status(database, job_id) == "PAUSED"


def test_collection_analyzer_honors_cancel(config, database, tmp_path):
    # A collections analyzer that had no job_id before this change now honors cancellation.
    root = tmp_path / "src"
    root.mkdir()
    (root / "download0.bin").write_bytes(b"a")
    (root / "download1.bin").write_bytes(b"b")
    DriveScanner(database, config).scan(root, incremental=False)
    job_id = create_job(database, "DIRECTORY_SUMMARY")
    update_job(database, job_id, "RUNNING")
    request_cancel(database, job_id)
    with pytest.raises(JobCancelled):
        run_acquisition_batch_analysis(database, config, job_id=job_id)
    assert _status(database, job_id) == "CANCELLED"


def test_analyzers_still_run_without_a_job(config, database, tmp_path):
    # The job_id path is optional: the same analyzers complete normally when called without one.
    _two_overlapping_dirs(config, database, tmp_path)
    run_directory_overlap_analysis(database, config)  # no job_id
    result = run_acquisition_batch_analysis(database, config)  # no job_id
    assert isinstance(result, dict)
