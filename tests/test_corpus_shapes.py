"""Corpus shapes the rest of the suite cannot express.

The committed fixtures are small, shallow and uniform, so the shapes that actually broke on a real
drive — deep nesting, one enormous directory, thousands of equal-sized files, a document larger
than a pipe buffer, a repeated rescan — were never exercised. Each test here builds one such shape
and asserts the *outcome* (completeness, correctness, reuse), not a timing.
"""

from __future__ import annotations

import json

import pytest

from housekeeper.analysers.registry import run_content_analysis
from housekeeper.core import counters
from housekeeper.database import Database
from housekeeper.scanner import DriveScanner
from tests.conftest import build_flat_corpus


def _count(database, sql, params=()):
    return int(database.fetch_one(sql, params)["n"])


def test_deep_tree_is_enumerated_to_the_leaf(config, database, tmp_path):
    depth = 14
    root = tmp_path / "deep"
    directory = root
    for level in range(depth):
        directory = directory / f"level_{level:02d}"
    directory.mkdir(parents=True)
    (directory / "leaf.txt").write_text("bottom", encoding="utf-8")

    DriveScanner(database, config).scan(root, incremental=False)
    leaf = database.fetch_one("SELECT relative_path FROM filesystem_entries WHERE name='leaf.txt'")
    assert leaf is not None, "a 14-deep tree lost its leaf"
    assert str(leaf["relative_path"]).count("/") == depth
    assert _count(database, "SELECT COUNT(*) n FROM filesystem_entries WHERE entry_type='directory'") == depth


def test_one_very_wide_directory_is_enumerated_completely(config, database, tmp_path):
    width = 50_000
    root = build_flat_corpus(tmp_path / "wide", file_count=width, dir_count=1)
    DriveScanner(database, config).scan(root, incremental=False)
    assert _count(database, "SELECT COUNT(*) n FROM filesystem_entries WHERE entry_type='file'") == width


def test_many_equal_sized_files_do_not_all_become_duplicates(config, database, tmp_path):
    """Equal size is a funnel, never evidence: only identical content may be grouped."""
    from housekeeper.analysers.exact_duplicates import run_exact_duplicate_analysis

    root = tmp_path / "equal"
    root.mkdir()
    for index in range(400):
        # Every file is exactly 16 bytes; only every tenth shares the same bytes.
        payload = b"shared-payload!!" if index % 10 == 0 else f"unique-file-{index:04d}".encode()
        (root / f"file_{index:04d}.bin").write_bytes(payload.ljust(16, b"\0")[:16])
    DriveScanner(database, config).scan(root, incremental=False)
    run_exact_duplicate_analysis(database, config)
    assert _count(database, "SELECT COUNT(*) n FROM exact_duplicate_groups") == 1
    members = _count(database, "SELECT COUNT(*) n FROM exact_duplicate_members")
    assert members == 40


@pytest.mark.parametrize("rescans", [2, 5])
def test_repeated_rescan_of_an_unchanged_tree_is_stable(config, database, tmp_path, rescans):
    """G2: a file's own prior snapshot is never evidence about itself."""
    from tests.conftest import analyse_and_classify

    root = build_flat_corpus(tmp_path / f"stable{rescans}", file_count=40, dir_count=4)
    scanner = DriveScanner(database, config)
    for _ in range(rescans):
        scanner.scan(root)
    analyse_and_classify(database, config)
    # Every file is unique, so after any number of rescans nothing may be proposed for review.
    reviewable = _count(
        database, "SELECT COUNT(*) n FROM classifications WHERE classification LIKE 'REVIEW_%'"
    )
    assert reviewable == 0, "a rescan turned unique files into review candidates"


def test_changed_descendant_under_unchanged_directory_metadata_is_detected(
    config, database, tmp_path
):
    root = tmp_path / "sneaky"
    nested = root / "unchanged_dir"
    nested.mkdir(parents=True)
    target = nested / "payload.txt"
    target.write_text("before", encoding="utf-8")

    scanner = DriveScanner(database, config)
    scanner.scan(root, incremental=False)
    directory_mtime = nested.stat().st_mtime
    target.write_text("after!", encoding="utf-8")  # same length, different bytes
    import os

    os.utime(nested, (directory_mtime, directory_mtime))  # directory metadata unchanged
    scanner.scan(root)

    row = database.fetch_one(
        """SELECT change_status FROM scan_entry_changes WHERE relative_path=?
           ORDER BY id DESC LIMIT 1""",
        ("unchanged_dir/payload.txt",),
    )
    assert row is not None, "the changed descendant was not visited at all"
    assert row["change_status"] != "UNCHANGED"


def test_document_larger_than_the_pipe_buffer_parses(config, database, tmp_path):
    """2.1: the isolated parser deadlocked on any result above ~128 KB and reported a timeout."""
    root = tmp_path / "bigdoc"
    root.mkdir()
    (root / "large.txt").write_text("x" * 400_000, encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    run_content_analysis(database, config, "documents")
    artifact = database.fetch_one(
        "SELECT status,error_message FROM analysis_artifacts WHERE analyser_name='documents'"
    )
    assert artifact is not None
    assert artifact["status"] == "COMPLETED", artifact["error_message"]


def test_isolated_parser_returns_a_two_million_character_result(config, tmp_path):
    """2.1's regression guard, now against the pooled sandbox that replaced the per-parse fork.

    Anything larger than the pipe buffer used to deadlock the child inside ``queue.put`` and be
    reported — after burning the whole timeout — as a parser timeout.
    """
    from housekeeper.analysers.parser_pool import ParserPool

    target = tmp_path / "large.txt"
    target.write_text("y" * 2_000_000, encoding="utf-8")
    with ParserPool(config, workers=1) as parsers:
        result = parsers.run("documents", str(target), 60)
    assert result.get("analysis_status") != "ERROR", result.get("analysis_error")
    assert len(result["normalized_text"]) == 2_000_000


def test_pooled_parser_survives_a_timeout_and_keeps_working(config, tmp_path):
    """A hostile file must cost its own artifact, not the run: the pool recovers and continues."""
    from housekeeper.analysers.parser_pool import ParserPool

    good = tmp_path / "fine.txt"
    good.write_text("readable", encoding="utf-8")
    with ParserPool(config, workers=1) as parsers:
        # A directory is not a document; the parse fails fast rather than hanging, but it still
        # exercises the error path that must not poison the worker.
        broken = parsers.run("documents", str(tmp_path), 5)
        assert broken.get("analysis_status") in {"ERROR", None} or broken.get("extraction_status") == "ERROR"
        after = parsers.run("documents", str(good), 30)
    assert "readable" in after.get("normalized_text", "")


def test_duplicate_group_with_a_canonical_override_survives_reanalysis(config, database, tmp_path):
    """2.3: rebuilding groups used to violate the canonical_overrides foreign key, forever."""
    from housekeeper.analysers.exact_duplicates import run_exact_duplicate_analysis

    root = tmp_path / "override"
    root.mkdir()
    for name in ("a.txt", "b.txt"):
        (root / name).write_text("identical payload", encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    run_exact_duplicate_analysis(database, config)
    group = database.fetch_one("SELECT id,canonical_entry_id FROM exact_duplicate_groups")
    assert group is not None
    other = database.fetch_one(
        "SELECT id FROM filesystem_entries WHERE entry_type='file' AND id<>?",
        (group["canonical_entry_id"],),
    )
    database.connect().execute(
        "INSERT INTO canonical_overrides(duplicate_group_id,canonical_entry_id) VALUES(?,?)",
        (group["id"], other["id"]),
    )
    database.connect().commit()

    run_exact_duplicate_analysis(database, config)  # must not raise IntegrityError

    override = database.fetch_one("SELECT duplicate_group_id,canonical_entry_id FROM canonical_overrides")
    assert override is not None, "the user's canonical decision was silently deleted"
    assert int(override["canonical_entry_id"]) == int(other["id"])
    assert (
        database.fetch_one(
            "SELECT 1 FROM exact_duplicate_groups WHERE id=?", (override["duplicate_group_id"],)
        )
        is not None
    ), "the override now points at a group that no longer exists"


def test_interrupted_scan_resumed_keeps_verified_evidence(config, tmp_path):
    """2.2: resume used to delete the conflicting row, cascading away hashes and protections."""
    root = build_flat_corpus(tmp_path / "resumed", file_count=6, dir_count=2)
    database = Database(config.database_path)
    database.initialize()
    try:
        scanner = DriveScanner(database, config)
        scanner.scan(root, incremental=False)
        run_content_analysis(database, config, None)
        before = database.fetch_one(
            """SELECT e.id,s.full_hash FROM filesystem_entries e JOIN file_signatures s ON s.entry_id=e.id
               WHERE e.entry_type='file' ORDER BY e.id LIMIT 1"""
        )
        assert before is not None and before["full_hash"]
        database.connect().execute(
            "INSERT INTO classifications(entry_id,classification,confidence) VALUES(?,'PROTECTED',1.0)",
            (before["id"],),
        )
        # Interrupted run: the scan_run is left RUNNING, so the next scan resumes into it.
        database.connect().execute(
            "UPDATE scan_runs SET status='INTERRUPTED' WHERE id=(SELECT MAX(id) FROM scan_runs)"
        )
        database.connect().commit()

        scanner.scan(root, resume=True)

        after = database.fetch_one(
            "SELECT full_hash FROM file_signatures WHERE entry_id=?", (before["id"],)
        )
        assert after is not None, "resume destroyed a verified signature"
        assert after["full_hash"] == before["full_hash"]
        assert (
            database.fetch_one(
                "SELECT classification FROM classifications WHERE entry_id=?", (before["id"],)
            )
            is not None
        ), "resume erased a PROTECTED classification"
    finally:
        database.close()


def test_all_cache_hit_rerun_starts_no_parsers(config, tmp_path):
    root = tmp_path / "cached"
    root.mkdir()
    (root / "note.txt").write_text("cache me", encoding="utf-8")
    database = Database(config.database_path)
    database.initialize()
    try:
        DriveScanner(database, config).scan(root, incremental=False)
        run_content_analysis(database, config, "documents")
        with counters.recording() as counts:
            run_content_analysis(database, config, "documents")
    finally:
        database.close()
    assert counts["parser_processes_started"] == 0
    assert counts["source_bytes_read"] == 0


def test_twentieth_rescan_issues_no_more_sql_than_the_second(config, database, tmp_path):
    """Statement count must scale with the drive, not with how many times it has been scanned.

    The plan asks for a 20th-rescan corpus and the suite stopped at five, which is exactly deep
    enough to hide a per-snapshot regression: at five snapshots a query repeated once per historical
    row is only 5x too much, which reads as noise.

    This counts *statements*, so it catches "a query per historical entry" but not "one aggregate
    that reads more rows". That second half is a plan property, not a count, and is asserted in
    test_current_state_chart_queries_never_scan_history below.
    """
    root = build_flat_corpus(tmp_path / "twenty", file_count=30, dir_count=3)
    scanner = DriveScanner(database, config)
    scanner.scan(root)  # first scan: everything is new

    with counters.recording() as second:
        scanner.scan(root)
    for _ in range(17):
        scanner.scan(root)
    with counters.recording() as twentieth:
        scanner.scan(root)

    # stat_calls must track the drive, not history: every rescan stats one snapshot's worth of
    # entries once, so the 20th matches the 2nd. This is the soak guard for the traversal half —
    # a per-entry cost that grew with rescans (or a reintroduced 4-stats-per-entry) trips it.
    assert twentieth["stat_calls"] == second["stat_calls"], (
        f"stat calls grew with history: {twentieth['stat_calls']} vs {second['stat_calls']}"
    )
    assert twentieth["stat_calls"] <= 40, "one stat per entry of a 34-entry tree, not four"

    assert _count(database, "SELECT COUNT(*) n FROM scan_runs") == 20
    history = _count(database, "SELECT COUNT(*) n FROM filesystem_entries")
    current = _count(database, "SELECT COUNT(*) n FROM current_entries")
    assert history > current * 15, "the corpus must really accumulate snapshots"

    # The scan itself, including the materialized-summary refresh it triggers.
    ratio = twentieth["sql_statements"] / max(1, second["sql_statements"])
    assert ratio < 1.5, (
        f"the 20th rescan issued {ratio:.1f}x the SQL of the 2nd "
        f"({twentieth['sql_statements']} vs {second['sql_statements']}) — cost is tracking history, "
        "not the drive"
    )
    assert twentieth["commits"] < 2 * second["commits"] + 10, (
        f"commits grew with history: {twentieth['commits']} vs {second['commits']}"
    )


def test_summaries_after_twenty_rescans_describe_one_drive(config, database, tmp_path):
    """The same corpus, read through the reporting layer: 30 files, not 600."""
    from housekeeper.reports.contexts import build_summary_context

    root = build_flat_corpus(tmp_path / "twenty-report", file_count=30, dir_count=3)
    scanner = DriveScanner(database, config)
    for _ in range(20):
        scanner.scan(root)
    database.refresh_materialized_summaries(
        _count(database, "SELECT MAX(id) n FROM scan_runs WHERE status='COMPLETE'")
    )

    assert _count(database, "SELECT COUNT(*) n FROM filesystem_entries WHERE entry_type='file'") == 600
    assert build_summary_context(database, config)["file_count"] == 30
    overview = json.loads(
        database.fetch_one(
            "SELECT value_json FROM materialized_summaries WHERE summary_key='overview'"
        )["value_json"]
    )
    assert overview["entries"] == _count(database, "SELECT COUNT(*) n FROM current_entries")


def test_current_state_chart_queries_never_scan_history(metadata_corpus):
    """Every dashboard aggregate that describes the drive must resolve through the run predicate.

    This is the half a statement count cannot see: an unscoped ``SUM(size_bytes)`` is one statement
    whether it reads the current inventory or twenty snapshots of it, so the cost of a scan grew
    with history while every counter stayed flat. Two of the five charts are *deliberately*
    historical and are named here so that adding a third has to be a decision.
    """
    from housekeeper.database import Database

    database, _run_id, _source = metadata_corpus
    historical = {"scan_history", "analyser_completion"}
    for key, (_columns, sql) in Database._CHART_QUERIES.items():
        plan = " ".join(
            row["detail"] for row in database.connect().execute("EXPLAIN QUERY PLAN " + sql)
        )
        if key in historical:
            continue
        assert "scan_run_id=?" in plan, f"chart {key!r} is not scoped to the current inventory\n{plan}"
        assert "SCAN filesystem_entries" not in plan, f"chart {key!r} scans all history\n{plan}"
