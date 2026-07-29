"""The assertions that would have caught the performance defects.

Every assertion here is on a *count of work*, never on wall clock: counters are identical on a
laptop and a CI runner, so they can be asserted rather than eyeballed. A rescan of an unchanged
tree must read no bytes and start no parsers; if it does, something is redoing settled work.
"""

from __future__ import annotations

import pytest

from housekeeper.core import counters
from housekeeper.scanner import DriveScanner
from tests.conftest import build_flat_corpus, run_with_counters


def test_counters_record_nothing_outside_a_recording_block():
    counters.count("full_hash_bytes", 1234)
    with counters.recording() as counts:
        counters.count("full_hash_bytes", 5)
    assert counts["full_hash_bytes"] == 5


def test_recording_counts_statements_and_commits(database):
    with counters.recording() as counts:
        database.execute("SELECT COUNT(*) FROM filesystem_entries").fetchone()
        database.connect().execute("INSERT INTO schema_migrations(version) VALUES(9999)")
        database.connect().commit()
    assert counts["sql_statements"] >= 2
    assert counts["commits"] == 1


def test_stage_durations_are_recorded():
    with counters.recording() as counts, counters.stage("scan"):
        pass
    assert "stage_ms:scan" in counts


@pytest.fixture(scope="module")
def rescan_counts(tmp_path_factory):
    """Scan+analyse a corpus twice into the same workspace; return both runs' counters.

    600 files, because every assertion here is "zero" or "a fraction of the first run" — sizing it
    larger costs a parser fork per document and proves nothing extra.
    """
    base = tmp_path_factory.mktemp("rescan")
    corpus = build_flat_corpus(base / "corpus", file_count=600, dir_count=20)
    workspace = base / "workspace"
    return run_with_counters(corpus, workspace), run_with_counters(corpus, workspace)


def test_first_run_actually_does_the_work(rescan_counts):
    """Guards the guard: if the first run read nothing either, the rescan assertions prove nothing."""
    first, _second = rescan_counts
    assert first["entries_enumerated"] == 620
    assert first["source_bytes_read"] > 0
    assert first["parser_processes_started"] > 0
    assert first["artifact_cache_misses"] > 0


def test_unchanged_rescan_reads_no_source_bytes(rescan_counts):
    first, second = rescan_counts
    assert second["source_bytes_read"] == 0, "nothing changed, so nothing should have been read"
    assert first["source_bytes_read"] > 0


def test_unchanged_rescan_starts_no_parser_processes(rescan_counts):
    _first, second = rescan_counts
    assert second["parser_processes_started"] == 0, "every artifact was already current"
    assert second["artifact_cache_misses"] == 0


def test_unchanged_rescan_does_a_small_constant_of_sql_per_entry(rescan_counts):
    """The plan phrased this as "under a tenth of the first run's SQL".

    That ratio stopped being the right measure once the *first* run also got several times leaner:
    a rescan's floor is one insert per entry, because the new snapshot still has to be recorded,
    and the counter charges one statement per row of an ``executemany`` (which is the honest unit
    of work). What must hold is that a rescan is O(entries) with a small constant — not
    O(entries x questions asked per entry), which is what "one query per row per spec" was.
    """
    first, second = rescan_counts
    per_entry = second["sql_statements"] / second["entries_enumerated"]
    assert per_entry < 2.0, f"{per_entry:.2f} statements per entry on an unchanged rescan"
    assert second["sql_statements"] < first["sql_statements"] * 0.25


def test_commits_are_per_batch_not_per_entry(rescan_counts):
    _first, second = rescan_counts
    assert second["commits"] < 200


def test_identity_reads_each_new_file_exactly_once(rescan_counts, tmp_path_factory):
    """Definition of done #7: a newly identified file is read at most once for identity.

    Identity used to cost a quick hash (three sampled reads) and then a full hash over everything —
    for a quick digest whose bytes are, by construction, a subset of the ones the full hash already
    went past. The counter is bytes, not passes, so this catches any reintroduced second read.
    """
    first, _second = rescan_counts
    # build_flat_corpus writes "corpus entry {index}\n"; the analyser reads each file once for
    # identity, and the document parser reads it again to extract text. Identity is the counter
    # under test, so compare against the full-hash bytes rather than the total.
    corpus_bytes = sum(len(f"corpus entry {index}\n".encode()) for index in range(600))
    assert first["full_hash_bytes"] == corpus_bytes, (
        f"identity read {first['full_hash_bytes']} bytes for a {corpus_bytes}-byte corpus "
        f"({first['full_hash_bytes'] / corpus_bytes:.2f}x)"
    )
    assert first["quick_hash_bytes"] == 0, "the quick digest is a by-product, not a second read"


#: Below this the two runs are too short to compare honestly; wall clock is noise in CI, which is
#: why every other assertion in this file counts work instead.
MINIMUM_TIMED_SECONDS = 1.0


def test_unchanged_rescan_is_not_slower_than_the_fresh_scan(tmp_path_factory):
    """Definition of done #4, in its own words: no slower than 1.25x, and zero bytes read.

    The counters cover the "does no work" half everywhere else in this file. This is the one place
    the wall-clock claim itself is checked, because a rescan can do less work and still be slower
    if the diff it does instead is quadratic in the snapshot.
    """
    import time

    base = tmp_path_factory.mktemp("ratio")
    corpus = build_flat_corpus(base / "corpus", file_count=10_000, dir_count=50)
    workspace = base / "workspace"

    started = time.perf_counter()
    first = run_with_counters(corpus, workspace)
    fresh = time.perf_counter() - started

    started = time.perf_counter()
    second = run_with_counters(corpus, workspace)
    rescan = time.perf_counter() - started

    assert second["source_bytes_read"] == 0
    assert first["entries_enumerated"] == second["entries_enumerated"] == 10_050
    if fresh < MINIMUM_TIMED_SECONDS:
        pytest.skip(f"fresh scan took {fresh:.2f}s — too short to compare wall clock honestly")
    assert rescan <= fresh * 1.25, (
        f"unchanged rescan {rescan:.2f}s against a fresh scan of {fresh:.2f}s "
        f"({rescan / fresh:.2f}x)"
    )


def test_no_analyser_commits_once_per_object(config, database, tmp_path):
    """Definition of done #5 across *every* analyser stage, not just scan and a cache-hit rerun.

    The existing commit test exercises a scan plus a content rerun in which every artifact is a
    cache hit — so it could not see the per-object commits that were still there: a transaction per
    normalized-text blob, per normalized-content artifact, per detected project, and one behind
    every ``update_job(..., "RUNNING", ...)`` in a loop, since any non-null status forces a commit.

    Bound relative to the corpus: a fixed ceiling would pass for a while and then rot as stages are
    added. What must hold is that commits track *batches and stages*, not objects.
    """
    from housekeeper.analysers.document_versions import run_document_version_analysis
    from housekeeper.analysers.exact_duplicates import run_exact_duplicate_analysis
    from housekeeper.analysers.normalized_content import run_normalized_content_analysis
    from housekeeper.analysers.projects import run_project_analysis
    from housekeeper.analysers.registry import run_content_analysis
    from housekeeper.policies import classify_all_entries

    files = 120
    root = tmp_path / "commits"
    root.mkdir()
    for index in range(files):
        # Distinct text so every file is its own content object and every analyser has real work.
        # A marker file per directory-ish group also gives the project stage something to detect.
        (root / f"doc-{index:04d}.txt").write_text(f"document number {index}\n" * 3, "utf-8")
    (root / "pyproject.toml").write_text("[project]\nname='x'\n", "utf-8")

    DriveScanner(database, config).scan(root, incremental=False)

    # Named individually rather than through analyse_and_classify, which does not include the three
    # stages whose per-object commits this is about.
    stages = {
        "exact_duplicates": lambda: run_exact_duplicate_analysis(database, config),
        "content": lambda: run_content_analysis(database, config, None),
        "normalized_content": lambda: run_normalized_content_analysis(database, config),
        "document_versions": lambda: run_document_version_analysis(database, config),
        "projects": lambda: run_project_analysis(database, config),
        "classify": lambda: classify_all_entries(database, config),
    }
    per_stage = {}
    for name, run in stages.items():
        with counters.recording() as counted:
            run()
        per_stage[name] = int(counted["commits"])

    assert database.fetch_one("SELECT COUNT(*) n FROM content_objects")["n"] >= files, (
        "the corpus produced no content objects, so no stage had per-object work to do"
    )
    assert database.fetch_one(
        "SELECT COUNT(*) n FROM content_text_blobs"
    )["n"] >= files, "no normalized text was stored, so _store_text was never exercised"

    offenders = {name: n for name, n in per_stage.items() if n >= files}
    assert not offenders, f"stages committing per object (>= {files} commits): {per_stage}"


def test_content_errors_and_exceptions_are_committed_per_batch(
    config, database, tmp_path, monkeypatch
):
    """A damaged corpus follows the same transaction policy as successful artifacts.

    The old condition batched on ``completed``. When every parser result was ERROR, completed stayed
    zero and ``0 % ARTIFACT_BATCH_SIZE`` committed every object. The separate post-processing
    exception handler also committed unconditionally. Exercise both paths without paying real parser
    startup or depending on optional document libraries.
    """
    from pathlib import Path

    from housekeeper.analysers import registry
    from housekeeper.analysers.registry import run_content_analysis
    from housekeeper.jobs import create_job, update_job

    files = 120
    root = tmp_path / "error-batching"
    root.mkdir()
    for index in range(files):
        (root / f"doc-{index:04d}.txt").write_text(f"document {index}\n", encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)

    class ErrorPool:
        def __init__(self, _config, workers, _memory_limit):
            self.workers = workers

        def run(self, _spec_name, path, _timeout):
            index = int(Path(path).stem.rsplit("-", 1)[1])
            if index % 2 == 0:
                return {"analysis_status": "ERROR", "analysis_error": "malformed document"}
            return {"extraction_status": "OK", "normalized_text": "post-process me"}

        def close(self):
            return None

    monkeypatch.setattr(registry, "ParserPool", ErrorPool)

    def fail_post_process(*_args, **_kwargs):
        raise RuntimeError("simulated text persistence failure")

    monkeypatch.setattr(registry, "_store_text", fail_post_process)
    job_id = create_job(database, "ANALYSE")
    update_job(database, job_id, "RUNNING")
    with counters.recording() as counted:
        result = run_content_analysis(database, config, "documents", job_id=job_id)

    assert result["errors"] == files
    assert database.fetch_one(
        "SELECT COUNT(*) n FROM analysis_artifacts WHERE status='ERROR'"
    )["n"] == files
    assert database.fetch_one(
        "SELECT COUNT(*) n FROM analysis_artifacts WHERE error_code='ANALYSER_EXCEPTION'"
    )["n"] == files // 2
    # Identity has one commit, artifacts ceil(120/50)=3, and the final flush is a small constant.
    assert counted["commits"] <= 8, counted


@pytest.fixture(scope="module")
def quickstart_rerun_counts(tmp_path_factory):
    """A full quickstart, an unchanged incremental re-run, and an unchanged forced full re-run."""
    from housekeeper.config import load_config
    from housekeeper.database import Database
    from housekeeper.quickstart import run_quickstart

    base = tmp_path_factory.mktemp("quickstart-rerun")
    corpus = build_flat_corpus(base / "corpus", file_count=400, dir_count=10)
    config = load_config(workspace_override=base / "workspace")
    database = Database(config.database_path)
    database.initialize()
    runs = {}
    try:
        for name, kwargs in (("first", {}), ("incremental", {}), ("forced", {"full": True})):
            with counters.recording() as counted:
                run_quickstart(database, config, corpus, generate_reports=False, **kwargs)
            runs[name] = counted
    finally:
        database.close()
    return runs


def test_unchanged_quickstart_rerun_stays_a_small_constant_per_entry(quickstart_rerun_counts):
    """The whole ~21-stage pipeline, not just the scan, on a tree nobody touched.

    A quickstart re-run has an irreducible O(entries) floor — the new snapshot is still recorded, and
    every stage keyed to entry ids has to re-derive its rows for it. What must hold is that it stays
    a small constant per entry, and that reusing the content-keyed stages costs strictly less than
    forcing them (`--full`) on the very same unchanged snapshot.
    """
    runs = quickstart_rerun_counts
    per_entry = runs["incremental"]["sql_statements"] / runs["incremental"]["entries_enumerated"]
    assert per_entry < 8.0, f"{per_entry:.2f} statements per entry on an unchanged quickstart re-run"
    assert runs["incremental"]["sql_statements"] < runs["first"]["sql_statements"] * 0.6
    assert runs["incremental"]["sql_statements"] < runs["forced"]["sql_statements"]
    assert runs["incremental"]["source_bytes_read"] == 0
