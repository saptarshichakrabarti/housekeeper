"""One-command pipeline: scan, analyse, classify, and report in a single invocation.

``housekeeper quickstart <source>`` (or ``make quickstart SOURCE=<source>``) runs the complete
*read-only* pipeline a new user would otherwise assemble from six commands. It follows the same
safety rules as the individual commands:

* nothing is ever moved, deleted, or modified — the pipeline only reads the source tree and writes
  the workspace inventory/reports;
* every step runs inside a durable job (pause/cancel-able, visible in ``jobs``/dashboard);
* optional-dependency analysers degrade to honest "unavailable" results instead of failing;
* re-running is safe and incremental — the scan reuses unchanged entries.

The strongest action the tool ever takes (moving approved files into a review folder) remains a
separate, explicit, manifest-verified command and is intentionally NOT part of quickstart.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from .analysers.scope import AnalyserScope
from .config import config_fingerprint
from .jobs import create_job, tracked_job, update_job
from .reuse import input_fingerprint, snapshot_token

# Stages whose output is keyed to *content objects*, so an earlier run's output still describes the
# current snapshot. Everything else is re-derived every run: a rescan writes new
# ``filesystem_entries`` rows, and anything keyed to an entry id (classifications, duplicate
# members, projects, canonical roles, directory overlap) would leave the ``current_*`` views empty
# if it were skipped. Content identity is global and copied forward by the scanner, so these stand.
REUSABLE_STAGES = frozenset({"content-analysis", "document-versions", "image-similarity"})


def _step(
    database,
    config,
    job_type: str,
    label: str,
    callback: Callable[[int | None], object],
    parent_job_id: int | None = None,
    fingerprint: str | None = None,
):
    """Run one pipeline step inside a durable tracked job and normalise its outcome."""
    scope: dict = {"quickstart": label}
    if fingerprint:
        # In the scope rather than a column of its own: scope_json is written once per job and never
        # rewritten, and the jobs table is a row per stage — a scan of it costs nothing.
        scope["input_fingerprint"] = fingerprint
    with tracked_job(
        database,
        job_type,
        scope,
        config_fingerprint(config),
        parent_job_id=parent_job_id,
    ) as job:
        return callback(job)


def _changed_entry_count(database, scan_run_id: int | None) -> int | None:
    """Entries this scan saw as changed, or ``None`` when it had no baseline to compare against.

    A first scan of a source records no change rows at all, which must never read as "nothing
    changed" — hence ``None`` rather than 0.
    """
    if scan_run_id is None:
        return None
    row = database.fetch_one(
        "SELECT COUNT(*) total, COALESCE(SUM(change_status<>'UNCHANGED'),0) changed "
        "FROM scan_entry_changes WHERE scan_run_id=?",
        (scan_run_id,),
    )
    if not row or not int(row["total"]):
        return None
    return int(row["changed"])


def _previous_pipeline_completed(database, source_root: Path, pipeline_job: int) -> bool:
    """Did the last quickstart of this source finish cleanly?

    Gates only the changed-only narrowing of content analysis, which assumes the previous run did the
    work its change record implies. After an interrupted, cancelled or failed run some of that work is
    still owed and is invisible in the change record. Stage reuse has its own, stronger evidence — a
    COMPLETED stage job — and is not gated on this, which is what lets a resume continue.
    """
    row = database.fetch_one(
        "SELECT status FROM jobs WHERE job_type='QUICKSTART' AND id<>? "
        "AND json_extract(scope_json,'$.source_root')=? ORDER BY id DESC LIMIT 1",
        (pipeline_job, str(source_root)),
    )
    return bool(row and row["status"] == "COMPLETED")


def _reusable_job(database, job_type: str, fingerprint: str) -> int | None:
    row = database.fetch_one(
        "SELECT id FROM jobs WHERE status='COMPLETED' AND job_type=? "
        "AND json_extract(scope_json,'$.input_fingerprint')=? ORDER BY id DESC LIMIT 1",
        (job_type, fingerprint),
    )
    return int(row["id"]) if row else None


def _record_skipped_stage(
    database,
    config,
    job_type: str,
    label: str,
    fingerprint: str,
    reused_job_id: int,
    pipeline_job: int,
) -> dict:
    """A stage that was considered and not run still gets a job row.

    Skipped work is reported as skipped: the Jobs page shows the stage was weighed and reused,
    rather than silently missing from the run.
    """
    job = create_job(
        database,
        job_type,
        {"quickstart": label, "input_fingerprint": fingerprint, "reused_job_id": reused_job_id},
        config_fingerprint(config),
        parent_job_id=pipeline_job,
    )
    update_job(
        database, job, "COMPLETED", skip_count=1, current_item=f"reused job {reused_job_id}"
    )
    return {"skipped_stage": f"reused job {reused_job_id}"}


def run_quickstart(
    database,
    config,
    source_root: Path,
    generate_reports: bool = True,
    progress: Callable[[str, int, int], None] = lambda message, stage, stage_total: None,
    full: bool = False,
    resumes: int | None = None,
) -> dict:
    """Execute the full safe pipeline against ``source_root`` and return a summary.

    The whole run is one durable ``QUICKSTART`` job with a child job per stage. Pause/cancel on
    any of those rows stops the entire run (control requests escalate to the pipeline root, and
    every stage polls its lineage), rather than only the stage that happened to be running.

    A re-run is incremental unless ``full``: stages whose inputs are unchanged since a completed run
    are reused rather than recomputed, and content analysis narrows to changed entries.
    """
    # Fail fast on a source that cannot be scanned, instead of reporting a hollow "complete" with
    # all-zero totals.
    if not source_root.exists():
        raise FileNotFoundError(f"source path does not exist: {source_root}")
    if not source_root.is_dir():
        raise NotADirectoryError(f"source must be a directory, not a file: {source_root}")
    scope: dict = {"source_root": str(source_root)}
    if resumes is not None:
        # Which terminal run this one continues. The old row stays terminal; the link lives here.
        scope["resumes"] = resumes
    with tracked_job(
        database, "QUICKSTART", scope, config_fingerprint(config)
    ) as pipeline_job:
        return _run_pipeline(
            database, config, source_root, generate_reports, progress, pipeline_job, full, resumes
        )


def _run_pipeline(
    database,
    config,
    source_root: Path,
    generate_reports: bool,
    progress: Callable[[str, int, int], None],
    pipeline_job: int,
    full: bool = False,
    resumes: int | None = None,
) -> dict:
    from .analysers.archive_equivalence import run_archive_directory_analysis
    from .analysers.backup_lineage import run_backup_lineage_analysis
    from .analysers.contact_sheets import run_contact_sheet_generation
    from .analysers.cross_format_derivation import run_cross_format_derivation_analysis
    from .analysers.directory_overlap import run_directory_overlap_analysis
    from .analysers.document_versions import run_document_version_analysis
    from .analysers.exact_duplicates import run_exact_duplicate_analysis
    from .analysers.images import run_image_analysis
    from .analysers.lifecycle import run_lifecycle_analysis
    from .analysers.normalized_content import run_normalized_content_analysis
    from .analysers.preservation_risk import run_preservation_risk_analysis
    from .analysers.projects import run_project_analysis
    from .analysers.registry import run_content_analysis
    from .analysers.review_priority import run_review_priority_analysis
    from .canonical.roles import assign_canonical_roles
    from .collections.events import run_photo_event_analysis
    from .collections.marginal_value import run_backup_value_analysis
    from .collections.record_series import run_record_series_analysis
    from .policies import classify_all_entries
    from .reporting import generate_all_reports
    from .scanner import DriveScanner

    # Named (rather than left as inline loop literals) so the stage count below is derived from
    # their length instead of a hand-maintained magic number.
    STRUCTURAL_ANALYSERS = (
        ("directory-overlap", "DIRECTORY_OVERLAP", run_directory_overlap_analysis),
        ("document-versions", "VERSION_ANALYSIS", run_document_version_analysis),
        ("image-similarity", "IMAGE_ANALYSIS", run_image_analysis),
        ("projects", "PROJECT_ANALYSIS", run_project_analysis),
        ("backup-lineage", "DIRECTORY_SUMMARY", run_backup_lineage_analysis),
        ("normalized-content", "CONTENT_ANALYSIS", run_normalized_content_analysis),
        ("derivations", "VERSION_ANALYSIS", run_cross_format_derivation_analysis),
    )
    POST_CANONICAL_ANALYSERS = (
        ("archive-of-directory", "ARCHIVE_ANALYSIS", run_archive_directory_analysis),
        ("backup-value", "DIRECTORY_SUMMARY", run_backup_value_analysis),
        ("record-series", "CLASSIFICATION", run_record_series_analysis),
        ("preservation-risk", "PROJECT_ANALYSIS", run_preservation_risk_analysis),
        ("photo-events", "IMAGE_ANALYSIS", run_photo_event_analysis),
        ("contact-sheets", "CONTACT_SHEET_GENERATION", run_contact_sheet_generation),
    )
    # scan, exact-duplicates, content-analysis, canonical-roles, classify, review-priority,
    # lifecycle, refresh-summaries.
    FIXED_STAGE_COUNT = 8
    stage_total = (
        FIXED_STAGE_COUNT
        + len(STRUCTURAL_ANALYSERS)
        + len(POST_CANONICAL_ANALYSERS)
        + (1 if generate_reports else 0)
    )

    steps: list[dict] = []
    summary: dict = {"source_root": str(source_root), "steps": steps}
    stage = 0

    def begin_stage(label: str) -> None:
        # Fired at the *start* of a stage (not on completion) so a live progress display's label
        # always names whatever is actively running.
        nonlocal stage
        stage += 1
        progress(f"[quickstart] {label}", stage, stage_total)
        # Mirror the same progress onto the pipeline job row: the Jobs table then shows one
        # QUICKSTART row advancing stage-by-stage, and each stage boundary refreshes the root's
        # heartbeat. Committed by the stage's own first status transition moments later.
        update_job(
            database,
            pipeline_job,
            processed_count=stage - 1,
            total_estimate=stage_total,
            current_item=label,
        )

    def record(label: str, result: object) -> None:
        steps.append({"step": label, "result": result})

    # Set after the scan, once there is a snapshot to fingerprint against.
    mode: dict = {"incremental": False, "changed_only": False, "token": ""}

    def step(job_type: str, label: str, callback: Callable[[int | None], object]) -> object:
        # Every stage job is a child of the pipeline job, so one pause/cancel controls the run.
        # Recorded whenever the stage is reusable *in principle*, including on a full run: the
        # fingerprint describes the inputs this job worked from, and a later run is what decides
        # whether to trust it. Only the lookup is gated on the mode.
        fingerprint = (
            input_fingerprint(label, mode["token"], config_fingerprint(config))
            if mode["token"] and label in REUSABLE_STAGES
            else None
        )
        if fingerprint and mode["incremental"]:
            reused = _reusable_job(database, job_type, fingerprint)
            if reused is not None:
                return _record_skipped_stage(
                    database, config, job_type, label, fingerprint, reused, pipeline_job
                )
        return _step(
            database,
            config,
            job_type,
            label,
            callback,
            parent_job_id=pipeline_job,
            fingerprint=fingerprint,
        )

    begin_stage(f"scanning {source_root} (read-only; nothing is ever moved)")
    # The scanner creates and completes its own durable SCAN job, so it is not wrapped in
    # ``tracked_job`` (which would double-manage the lifecycle); it parents that job itself.
    scanner = DriveScanner(database, config)
    record("scan", scanner.scan(source_root, parent_job_id=pipeline_job))
    # Scope every analysis to the run just produced. The tool keeps scan history, so an unscoped
    # exact-duplicate pass would group a file with its own prior-scan snapshot and then classify the
    # current copy as a removable duplicate — marking unique, single-copy files REVIEW_SAFE on a
    # routine re-run. Scoping to this scan run keeps re-runs safe and idempotent.
    scan_run_id = scanner.last_run_id
    scope = (
        AnalyserScope.for_run(scan_run_id)
        if scan_run_id is not None
        else AnalyserScope.current(database)
    )
    # Two separate decisions, because they rest on different evidence.
    #
    # *Stage reuse* only ever fires on a fingerprint hit, and a fingerprint is only recorded on a
    # stage job that reached COMPLETED. An interrupted run therefore cannot cause a stage to be
    # skipped — the stages it did finish are exactly the ones worth skipping. This is what makes
    # Resume continue a run rather than redo it, so it deliberately does *not* require the previous
    # pipeline to have completed.
    #
    # *Changed-only narrowing* of content analysis has no such per-unit evidence: an artifact the
    # previous run still owed is invisible in the change record, and narrowing to changed entries
    # would skip it forever. That one does require a cleanly completed previous pipeline.
    #
    # ponytail: no "changed is small relative to inventory" threshold — a knob nobody could set
    # meaningfully. Narrowing is right whatever the ratio.
    changed_entries = _changed_entry_count(database, scan_run_id)
    reuse_enabled = (
        not full
        and changed_entries is not None
        and bool(config.section("incremental")["reuse_unchanged_stages"])
    )
    mode["incremental"] = reuse_enabled
    mode["changed_only"] = reuse_enabled and _previous_pipeline_completed(
        database, source_root, pipeline_job
    )
    if scan_run_id is not None:
        mode["token"] = snapshot_token(database, scan_run_id)
    summary["mode"] = "incremental" if mode["incremental"] else "full"
    summary["changed_entries"] = changed_entries
    summary["changed_only"] = mode["changed_only"]
    summary["resumes"] = resumes
    begin_stage("exact-duplicates")
    record(
        "exact-duplicates",
        step(
            "EXACT_DUPLICATES",
            "exact-duplicates",
            lambda job: run_exact_duplicate_analysis(database, config, job_id=job, scope=scope),
        ),
    )
    begin_stage("content-analysis")
    record(
        "content-analysis",
        step(
            "CONTENT_ANALYSIS",
            "content-analysis",
            lambda job: run_content_analysis(
                database, config, None, scope, changed_only=mode["changed_only"], job_id=job
            ),
        ),
    )
    # Each factory binds ``runner`` as a fresh parameter (avoiding late-binding over the loop) and
    # returns a single-argument step callback. *Every* analyser receives the current-run scope, not
    # just the structural ones: an analyser that can be called without a scope will be, and the six
    # post-canonical stages used to get only a job id and so re-derived all of scan history.
    def scope_positional(runner: Callable) -> Callable[[int | None], object]:
        return lambda job: runner(database, config, scope, job)

    def job_keyword(runner: Callable) -> Callable[[int | None], object]:
        return lambda job: runner(database, config, scope=scope, job_id=job)

    # Structural analysers over the fresh inventory (scope, then job_id, positionally).
    for label, job_type, runner in STRUCTURAL_ANALYSERS:
        begin_stage(label)
        record(label, step(job_type, label, scope_positional(runner)))
    begin_stage("canonical-roles")
    record(
        "canonical-roles",
        step(
            "CLASSIFICATION",
            "canonical-roles",
            lambda job: assign_canonical_roles(database),
        ),
    )
    for label, job_type, runner in POST_CANONICAL_ANALYSERS:
        begin_stage(label)
        record(label, step(job_type, label, job_keyword(runner)))
    begin_stage("classify")
    record(
        "classify",
        step(
            "CLASSIFICATION",
            "classify",
            lambda job: classify_all_entries(database, config, job_id=job, scope=scope),
        ),
    )
    begin_stage("review-priority")
    record(
        "review-priority",
        step(
            "CLASSIFICATION",
            "review-priority",
            lambda job: run_review_priority_analysis(database, config, scope, job_id=job),
        ),
    )
    begin_stage("lifecycle")
    record(
        "lifecycle",
        step(
            "CLASSIFICATION",
            "lifecycle",
            lambda job: run_lifecycle_analysis(database, config, scope, job_id=job),
        ),
    )
    # The scanner refreshed the dashboard's materialized summaries at scan end — before any of the
    # analysis above ran. Refresh again now that content objects, duplicate groups and
    # classifications exist, so the overview does not report zeros until a manual "Refresh now".
    begin_stage("refresh summaries")
    database.refresh_materialized_summaries(scan_run_id)
    if generate_reports:
        begin_stage("reports")
        report_paths = step(
            "REPORT_GENERATION",
            "reports",
            lambda job: [
                p.as_posix()
                for p in generate_all_reports(database, config, job_id=job, reuse=not full)
            ],
        )
        record("reports", report_paths)
        summary["reports"] = report_paths

    # Each scan is a historical snapshot (the tool keeps scan history), so the summary reports the
    # CURRENT inventory — the run this call actually wrote to (captured from the scanner, not
    # MAX(id), which is wrong when a scan resumes an earlier interrupted run).
    scan_run_id = scanner.last_run_id or 0

    def count(sql: str) -> int:
        # Every placeholder in these queries binds the current scan run id.
        row = database.fetch_one(sql, (scan_run_id,) * sql.count("?"))
        return int(row["n"]) if row else 0

    summary["scan_run_id"] = scan_run_id
    summary["totals"] = {
        "files": count(
            "SELECT COUNT(*) n FROM filesystem_entries WHERE entry_type='file' AND scan_run_id=?"
        ),
        "directories": count(
            "SELECT COUNT(*) n FROM filesystem_entries WHERE entry_type='directory' AND scan_run_id=?"
        ),
        "content_objects": count(
            "SELECT COUNT(DISTINCT l.content_object_id) n FROM entry_content_links l "
            "JOIN filesystem_entries e ON e.id=l.entry_id WHERE e.scan_run_id=?"
        ),
        # Duplicate counts are intra-current-scan: a group counts only when two or more of its
        # members are in the current snapshot. This keeps the number stable and meaningful across
        # re-runs (the tool keeps scan history, so a whole-database count would treat a file's own
        # prior snapshot as a duplicate of itself).
        "exact_duplicate_groups": count(
            "SELECT COUNT(*) n FROM (SELECT g.id FROM exact_duplicate_groups g "
            "JOIN exact_duplicate_members m ON m.group_id=g.id "
            "JOIN filesystem_entries e ON e.id=m.entry_id WHERE e.scan_run_id=? "
            "GROUP BY g.id HAVING COUNT(*)>=2)"
        ),
        # Redundant copies within the current snapshot: for each intra-scan group, its current-scan
        # members minus one representative. Independent of the global canonical (which may live in
        # an older snapshot), so it stays stable across re-runs.
        "duplicate_files": count(
            "SELECT COALESCE(SUM(cnt-1),0) n FROM (SELECT g.id, COUNT(*) cnt "
            "FROM exact_duplicate_groups g JOIN exact_duplicate_members m ON m.group_id=g.id "
            "JOIN filesystem_entries e ON e.id=m.entry_id WHERE e.scan_run_id=? "
            "GROUP BY g.id HAVING COUNT(*)>=2)"
        ),
        "protected": count(
            "SELECT COUNT(*) n FROM classifications c JOIN filesystem_entries e ON e.id=c.entry_id "
            "WHERE e.scan_run_id=? AND c.classification='PROTECTED'"
        ),
    }
    summary["workspace"] = str(config.workspace)
    # The three lines a re-run is actually about. Same context the `changes` report renders, so the
    # CLI epilogue and the report can never disagree — including on having nothing to compare.
    from .reports.contexts import changes_digest

    summary["changes"] = changes_digest(database, config)
    # Roll stage errors up onto the pipeline row so its terminal state is honest: the enclosing
    # tracked_job reads error_count and settles COMPLETED_WITH_ERRORS when any stage had errors.
    stage_errors = database.fetch_one(
        "SELECT COALESCE(SUM(error_count),0) n FROM jobs WHERE parent_job_id=?", (pipeline_job,)
    )
    update_job(
        database,
        pipeline_job,
        processed_count=stage,
        error_count=int(stage_errors["n"]) if stage_errors else 0,
        current_item="complete",
    )
    return summary


def next_steps(config) -> list[str]:
    """Human-readable follow-ups printed after a quickstart run. Advisory text only."""
    reports_dir = (config.workspace / config.data["workspace"]["reports_dir"]).as_posix()
    return [
        f"Browse the generated reports under {reports_dir}",
        "Start the local dashboard:  housekeeper dashboard   (or: make dashboard)",
        "Inspect the review queue:   housekeeper review list",
        (
            "Everything so far was read-only. Moving files stays a separate, explicit, "
            "manifest-verified flow: export-review -> validate-manifest -> move-to-review "
            "(dry-run first). Nothing is ever deleted."
        ),
    ]
