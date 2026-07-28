"""Progress reporting: derivation helpers (core/progress.py), total_estimate wiring across
analysers, GUI progress-cell rendering, and the CLI status-line formatter. All of these read the
same ``jobs`` rows, so the GUI and the CLI can never disagree about a job's progress.
"""

import pytest

from housekeeper.core.progress import (
    Progress,
    eta_seconds,
    format_duration,
    seconds_since,
    throughput,
)
from housekeeper.jobs import create_job, update_job
from housekeeper.progress_line import format_status_line


def test_progress_fraction_is_none_when_total_unknown():
    assert Progress(processed=5, total=None).fraction is None
    assert Progress(processed=5, total=10).fraction == 0.5


def test_throughput_never_divides_by_zero():
    assert throughput(10, 0) == 0.0
    assert throughput(10, -1) == 0.0
    assert throughput(100, 10) == 10.0


def test_eta_seconds_is_none_for_indeterminate_or_stalled_operations():
    assert eta_seconds(5, None, 10.0) is None  # unknown total: never fake an ETA
    assert eta_seconds(5, 10, 0.0) is None  # no measurable rate yet
    assert eta_seconds(5, 10, 5.0) == 1.0
    assert eta_seconds(10, 10, 5.0) == 0.0  # already done: zero, not negative


def test_format_duration_switches_to_hours_past_an_hour():
    assert format_duration(19) == "00:19"
    assert format_duration(3661) == "1:01:01"


def test_seconds_since_handles_missing_timestamp():
    assert seconds_since(None) == 0.0
    assert seconds_since("not a timestamp") == 0.0


def test_update_job_persists_and_revises_total_estimate(database):
    job_id = create_job(database, "TEST")
    update_job(database, job_id, total_estimate=100)
    row = database.fetch_one("SELECT total_estimate FROM jobs WHERE id=?", (job_id,))
    assert row["total_estimate"] == 100
    update_job(database, job_id, total_estimate=42)
    row = database.fetch_one("SELECT total_estimate FROM jobs WHERE id=?", (job_id,))
    assert row["total_estimate"] == 42


def test_exact_duplicate_analysis_sets_total_estimate(config, database, tmp_path):
    from housekeeper.analysers.exact_duplicates import run_exact_duplicate_analysis
    from housekeeper.scanner import DriveScanner

    root = tmp_path / "src"
    root.mkdir()
    for name in ("a.txt", "b.txt"):
        (root / name).write_text("same content", encoding="utf-8")
    (root / "unique.txt").write_text("unique", encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)

    job_id = create_job(database, "EXACT_DUPLICATES")
    update_job(database, job_id, "RUNNING")
    run_exact_duplicate_analysis(database, config, job_id=job_id)
    row = database.fetch_one("SELECT total_estimate FROM jobs WHERE id=?", (job_id,))
    # Revised for the second (grouping) phase: exactly one duplicate group.
    assert row["total_estimate"] == 1


def test_classify_sets_total_estimate_to_file_count(scanned):
    from housekeeper.policies import classify_all_entries

    database, config, _root = scanned
    job_id = create_job(database, "CLASSIFICATION")
    update_job(database, job_id, "RUNNING")
    classify_all_entries(database, config, job_id=job_id)
    expected = database.fetch_one(
        "SELECT COUNT(*) n FROM filesystem_entries WHERE entry_type='file'"
    )["n"]
    row = database.fetch_one(
        "SELECT total_estimate,processed_count FROM jobs WHERE id=?", (job_id,)
    )
    assert row["total_estimate"] == expected
    assert row["processed_count"] == expected


def test_generate_all_reports_sets_total_estimate(scanned):
    from housekeeper.reports.contexts import CONTEXT_BUILDERS
    from housekeeper.reports.generator import generate_all_reports

    database, config, _root = scanned
    job_id = create_job(database, "REPORT_GENERATION")
    update_job(database, job_id, "RUNNING")
    generate_all_reports(database, config, job_id=job_id)
    row = database.fetch_one(
        "SELECT total_estimate,processed_count FROM jobs WHERE id=?", (job_id,)
    )
    expected = len(CONTEXT_BUILDERS) + 2
    assert row["total_estimate"] == expected
    assert row["processed_count"] == expected


def test_format_status_line_is_determinate_indeterminate_or_pipeline_prefixed():
    determinate = {
        "job_type": "EXACT_DUPLICATES",
        "processed_count": 68,
        "total_estimate": 100,
        "current_item": None,
        "started_at": None,
    }
    line = format_status_line(determinate)
    assert "[exact_duplicates]" in line
    assert "68%" in line
    assert "68/100" in line

    indeterminate = {
        "job_type": "SCAN",
        "processed_count": 12431,
        "total_estimate": None,
        "current_item": "/some/dir",
        "started_at": None,
    }
    line = format_status_line(indeterminate)
    assert "%" not in line  # never a fabricated percentage
    assert "ETA" not in line  # never a fabricated ETA for an indeterminate operation
    assert "12,431 processed" in line
    assert "/some/dir" in line

    prefixed = format_status_line(determinate, stage_ref={"stage": 4, "total": 11})
    assert prefixed.startswith("Stage 4/11 · ")


@pytest.fixture
def dashboard_client(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from housekeeper.dashboard.app import create_app
    from housekeeper.database import Database

    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    return db, TestClient(create_app(db))


def test_jobs_fragment_renders_determinate_and_indeterminate_progress(dashboard_client):
    db, client = dashboard_client
    determinate_id = create_job(db, "EXACT_DUPLICATES")
    update_job(db, determinate_id, "RUNNING", processed_count=5, total_estimate=10)
    indeterminate_id = create_job(db, "SCAN")
    update_job(db, indeterminate_id, "RUNNING", processed_count=3, current_item="<b>/x</b>")

    body = client.get("/fragments/jobs").text
    assert "value='5' max='10'" in body
    assert "<progress></progress>" in body  # indeterminate: no value/max, never a fake percentage
    assert "&lt;b&gt;" in body  # current_item is escaped
    assert "<b>/x</b>" not in body


def test_jobs_fragment_marks_failed_job_progress_as_danger(dashboard_client):
    db, client = dashboard_client
    job_id = create_job(db, "SCAN")
    update_job(db, job_id, "FAILED", processed_count=3, total_estimate=10)
    body = client.get("/fragments/jobs").text
    assert "hk-progress--danger" in body
