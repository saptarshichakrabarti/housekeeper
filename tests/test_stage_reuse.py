"""Stage reuse: a stage whose inputs are provably identical to a completed run is not re-run.

The dangerous mistake this guards is reusing too much. A rescan writes a whole new set of
``filesystem_entries`` rows, so a stage keyed to entry ids (classify, exact-duplicates, projects)
must run every time or the ``current_*`` views — the entire dashboard and every report — come back
empty. Only content-object-keyed stages are reusable, and the totals of a reused run must match a
forced full run exactly.
"""

from __future__ import annotations

from housekeeper.config import load_config
from housekeeper.database import Database
from housekeeper.database_maintenance import purge_runs
from housekeeper.quickstart import REUSABLE_STAGES, run_quickstart
from housekeeper.reuse import input_fingerprint, snapshot_token


def _drive(tmp_path):
    root = tmp_path / "drive"
    (root / "Original").mkdir(parents=True)
    (root / "Backup").mkdir(parents=True)
    for name in ("one.txt", "two.txt", "three.txt"):
        (root / "Original" / name).write_text(f"shared {name}", encoding="utf-8")
        (root / "Backup" / name).write_text(f"shared {name}", encoding="utf-8")
    (root / "Original" / "notes.md").write_text("# unique\n", encoding="utf-8")
    return root


def _fresh(tmp_path, name="ws"):
    config = load_config(workspace_override=tmp_path / name)
    database = Database(config.database_path)
    database.initialize()
    return database, config


def _skipped(summary):
    return {
        step["step"]
        for step in summary["steps"]
        if isinstance(step["result"], dict) and step["result"].get("skipped_stage")
    }


def _run(database, config, root, **kwargs):
    return run_quickstart(database, config, root, generate_reports=False, **kwargs)


def test_first_run_is_full_and_reuses_nothing(tmp_path):
    database, config = _fresh(tmp_path)
    summary = _run(database, config, _drive(tmp_path))
    # No baseline: a first scan records no change rows, which must never read as "nothing changed".
    assert summary["mode"] == "full"
    assert summary["changed_entries"] is None
    assert _skipped(summary) == set()


def test_unchanged_rerun_reuses_content_keyed_stages_only(tmp_path):
    root = _drive(tmp_path)
    database, config = _fresh(tmp_path)
    first = _run(database, config, root)
    second = _run(database, config, root)
    assert second["mode"] == "incremental"
    assert second["changed_entries"] == 0
    assert _skipped(second) == set(REUSABLE_STAGES)
    # The point of the restriction: everything the dashboard reads is still there, and identical.
    assert second["totals"] == first["totals"]
    for view in ("current_classifications", "current_exact_duplicate_members"):
        assert database.fetch_one(f"SELECT COUNT(*) n FROM {view}")["n"] > 0, view
    # A third unchanged run still reuses: the token names the snapshot's content, not the run id,
    # so a chain of unchanged rescans keeps matching the run that last saw a change.
    assert _skipped(_run(database, config, root)) == set(REUSABLE_STAGES)


def test_a_changed_file_unskips_every_stage(tmp_path):
    root = _drive(tmp_path)
    database, config = _fresh(tmp_path)
    _run(database, config, root)
    _run(database, config, root)
    (root / "Original" / "notes.md").write_text("# unique, edited\n", encoding="utf-8")
    third = _run(database, config, root)
    assert third["mode"] == "incremental"
    assert third["changed_entries"] >= 1
    assert _skipped(third) == set()


def test_full_flag_forces_every_stage(tmp_path):
    root = _drive(tmp_path)
    database, config = _fresh(tmp_path)
    _run(database, config, root)
    forced = _run(database, config, root, full=True)
    assert forced["mode"] == "full"
    assert _skipped(forced) == set()


def test_configuration_change_unskips(tmp_path):
    root = _drive(tmp_path)
    database, config = _fresh(tmp_path)
    _run(database, config, root)
    assert _skipped(_run(database, config, root)) == set(REUSABLE_STAGES)
    config.section("document_similarity")["lsh_threshold"] = 0.5
    assert _skipped(_run(database, config, root)) == set()


def test_code_change_unskips(tmp_path, monkeypatch):
    root = _drive(tmp_path)
    database, config = _fresh(tmp_path)
    _run(database, config, root)
    assert _skipped(_run(database, config, root)) == set(REUSABLE_STAGES)
    # Standing in for an edit to any analyser: the fingerprint covers the code, so output produced
    # by different code is never reused.
    monkeypatch.setattr("housekeeper.reuse.code_fingerprint", lambda: "edited")
    assert _skipped(_run(database, config, root)) == set()


def test_purge_invalidates_reuse(tmp_path):
    root = _drive(tmp_path)
    database, config = _fresh(tmp_path)
    _run(database, config, root)
    _run(database, config, root)
    purge_runs(database, config)
    after = _run(database, config, root)
    # The jobs a fingerprint would match are gone with the runs they described, and the fresh scan
    # has no baseline again.
    assert after["mode"] == "full"
    assert _skipped(after) == set()


def test_an_interrupted_previous_run_stops_changed_only_narrowing(tmp_path):
    root = _drive(tmp_path)
    database, config = _fresh(tmp_path)
    _run(database, config, root)
    assert _run(database, config, root)["changed_only"] is True
    # An interrupted run may still owe artifacts, and an artifact that is owed is invisible in the
    # change record — so content analysis must consider every object, not just the changed ones.
    database.connect().execute(
        "UPDATE jobs SET status='INTERRUPTED' WHERE job_type='QUICKSTART' "
        "AND id=(SELECT MAX(id) FROM jobs WHERE job_type='QUICKSTART')"
    )
    database.connect().commit()
    after = _run(database, config, root)
    assert after["changed_only"] is False
    # Stage reuse is unaffected: it rests on each stage's own COMPLETED row.
    assert after["mode"] == "incremental"


def test_snapshot_token_is_stable_across_unchanged_rescans(tmp_path):
    root = _drive(tmp_path)
    database, config = _fresh(tmp_path)
    runs = [_run(database, config, root)["scan_run_id"] for _ in range(3)]
    tokens = {snapshot_token(database, run) for run in runs}
    assert len(tokens) == 1
    # Different labels never collide, so a reused job of one stage cannot satisfy another.
    token = tokens.pop()
    assert input_fingerprint("images", token, "cfg") != input_fingerprint("documents", token, "cfg")


def test_resume_after_an_interrupted_run_reuses_the_stages_that_finished(tmp_path):
    """Resume continues rather than redoes: a stage that COMPLETED is skipped by fingerprint.

    Stage reuse rests on the stage job's own terminal state, not on the pipeline's, so an interrupted
    run does not force the next one to redo the work it already finished. Changed-only narrowing is
    the part that does need a cleanly completed predecessor, and it stays off here.
    """
    root = _drive(tmp_path)
    database, config = _fresh(tmp_path)
    _run(database, config, root)
    # The run completed, then something killed the process: the pipeline row is INTERRUPTED while its
    # stage rows stay COMPLETED, exactly as the reaper leaves them.
    database.connect().execute(
        "UPDATE jobs SET status='INTERRUPTED' WHERE job_type='QUICKSTART' "
        "AND id=(SELECT MAX(id) FROM jobs WHERE job_type='QUICKSTART')"
    )
    database.connect().commit()

    resumed = _run(database, config, root, resumes=1)
    assert resumed["mode"] == "incremental"
    assert _skipped(resumed) == set(REUSABLE_STAGES)
    # ...but content analysis is not narrowed to changed entries, because the previous pipeline did
    # not finish and may still owe artifacts.
    assert resumed["changed_only"] is False
    assert resumed["resumes"] == 1
    assert resumed["totals"] == _run(database, config, root, full=True)["totals"]


def test_a_stage_that_did_not_finish_is_never_reused(tmp_path):
    root = _drive(tmp_path)
    database, config = _fresh(tmp_path)
    _run(database, config, root)
    # One stage was cancelled mid-run; its fingerprint must not authorise a skip.
    database.connect().execute(
        "UPDATE jobs SET status='CANCELLED' WHERE json_extract(scope_json,'$.quickstart')='image-similarity'"
    )
    database.connect().commit()
    skipped = _skipped(_run(database, config, root))
    assert "image-similarity" not in skipped
    assert skipped == set(REUSABLE_STAGES) - {"image-similarity"}
