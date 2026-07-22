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

from pathlib import Path
from typing import Callable

from .analysers.scope import analyserScope
from .config import config_fingerprint
from .jobs import tracked_job


def _step(database, config, job_type: str, label: str, callback: Callable[[int | None], object]):
    """Run one pipeline step inside a durable tracked job and normalise its outcome."""
    with tracked_job(database, job_type, {"quickstart": label}, config_fingerprint(config)) as job:
        return callback(job)


def run_quickstart(
    database,
    config,
    source_root: Path,
    generate_reports: bool = True,
    progress: Callable[[str, int, int], None] = lambda message, stage, stage_total: None,
) -> dict:
    """Execute the full safe pipeline against ``source_root`` and return a summary."""
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

    # Fail fast on a source that cannot be scanned, instead of reporting a hollow "complete" with
    # all-zero totals.
    if not source_root.exists():
        raise FileNotFoundError(f"source path does not exist: {source_root}")
    if not source_root.is_dir():
        raise NotADirectoryError(f"source must be a directory, not a file: {source_root}")

    # Named (rather than left as inline loop literals) so the stage count below is derived from
    # their length instead of a hand-maintained magic number.
    STRUCTURAL_analyseRS = (
        ("directory-overlap", "DIRECTORY_OVERLAP", run_directory_overlap_analysis),
        ("document-versions", "VERSION_ANALYSIS", run_document_version_analysis),
        ("image-similarity", "IMAGE_ANALYSIS", run_image_analysis),
        ("projects", "PROJECT_ANALYSIS", run_project_analysis),
        ("backup-lineage", "DIRECTORY_SUMMARY", run_backup_lineage_analysis),
        ("normalized-content", "CONTENT_ANALYSIS", run_normalized_content_analysis),
        ("derivations", "VERSION_ANALYSIS", run_cross_format_derivation_analysis),
    )
    POST_CANONICAL_analyseRS = (
        ("archive-of-directory", "ARCHIVE_ANALYSIS", run_archive_directory_analysis),
        ("backup-value", "DIRECTORY_SUMMARY", run_backup_value_analysis),
        ("record-series", "CLASSIFICATION", run_record_series_analysis),
        ("preservation-risk", "PROJECT_ANALYSIS", run_preservation_risk_analysis),
        ("photo-events", "IMAGE_ANALYSIS", run_photo_event_analysis),
        ("contact-sheets", "CONTACT_SHEET_GENERATION", run_contact_sheet_generation),
    )
    # scan, exact-duplicates, content-analysis, canonical-roles, classify, review-priority, lifecycle.
    FIXED_STAGE_COUNT = 7
    stage_total = (
        FIXED_STAGE_COUNT
        + len(STRUCTURAL_analyseRS)
        + len(POST_CANONICAL_analyseRS)
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

    def record(label: str, result: object) -> None:
        steps.append({"step": label, "result": result})

    begin_stage(f"scanning {source_root} (read-only; nothing is ever moved)")
    # The scanner creates and completes its own durable SCAN job, so it is not wrapped in
    # ``tracked_job`` (which would double-manage the lifecycle).
    scanner = DriveScanner(database, config)
    record("scan", scanner.scan(source_root))
    # Scope every analysis to the run just produced. The tool keeps scan history, so an unscoped
    # exact-duplicate pass would group a file with its own prior-scan snapshot and then classify the
    # current copy as a removable duplicate — marking unique, single-copy files REVIEW_SAFE on a
    # routine re-run. Scoping to this scan run keeps re-runs safe and idempotent.
    scan_run_id = scanner.last_run_id
    scope = analyserScope(scan_run_id=scan_run_id) if scan_run_id is not None else None
    begin_stage("exact-duplicates")
    record(
        "exact-duplicates",
        _step(
            database,
            config,
            "EXACT_DUPLICATES",
            "exact-duplicates",
            lambda job: run_exact_duplicate_analysis(database, config, job_id=job, scope=scope),
        ),
    )
    begin_stage("content-analysis")
    record(
        "content-analysis",
        _step(
            database,
            config,
            "CONTENT_ANALYSIS",
            "content-analysis",
            lambda job: run_content_analysis(database, config, None, job_id=job),
        ),
    )
    # Each factory binds ``runner`` as a fresh parameter (avoiding late-binding over the loop) and
    # returns a single-argument step callback. Structural analysers receive the current-run scope so
    # they never relate a file to its own prior-scan snapshot.
    def scope_positional(runner: Callable) -> Callable[[int | None], object]:
        return lambda job: runner(database, config, scope, job)

    def job_keyword(runner: Callable) -> Callable[[int | None], object]:
        return lambda job: runner(database, config, job_id=job)

    # Structural analysers over the fresh inventory (scope, then job_id, positionally).
    for label, job_type, runner in STRUCTURAL_analyseRS:
        begin_stage(label)
        record(label, _step(database, config, job_type, label, scope_positional(runner)))
    begin_stage("canonical-roles")
    record(
        "canonical-roles",
        _step(
            database,
            config,
            "CLASSIFICATION",
            "canonical-roles",
            lambda job: assign_canonical_roles(database),
        ),
    )
    for label, job_type, runner in POST_CANONICAL_analyseRS:
        begin_stage(label)
        record(label, _step(database, config, job_type, label, job_keyword(runner)))
    begin_stage("classify")
    record(
        "classify",
        _step(
            database,
            config,
            "CLASSIFICATION",
            "classify",
            lambda job: classify_all_entries(database, config, job_id=job),
        ),
    )
    begin_stage("review-priority")
    record(
        "review-priority",
        _step(
            database,
            config,
            "CLASSIFICATION",
            "review-priority",
            lambda job: run_review_priority_analysis(database, config, job_id=job),
        ),
    )
    begin_stage("lifecycle")
    record(
        "lifecycle",
        _step(
            database,
            config,
            "CLASSIFICATION",
            "lifecycle",
            lambda job: run_lifecycle_analysis(database, config, job_id=job),
        ),
    )
    if generate_reports:
        begin_stage("reports")
        report_paths = _step(
            database,
            config,
            "REPORT_GENERATION",
            "reports",
            lambda job: [p.as_posix() for p in generate_all_reports(database, config, job_id=job)],
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
    return summary


def next_steps(config) -> list[str]:
    """Human-readable follow-ups printed after a quickstart run. Advisory text only."""
    reports_dir = (config.workspace / config.data["workspace"]["reports_dir"]).as_posix()
    return [
        f"Browse the generated reports under {reports_dir}",
        "Start the local dashboard:  housekeeper dashboard   (or: make dashboard)",
        "Inspect the review queue:   housekeeper review list",
        "Everything so far was read-only. Moving files stays a separate, explicit,"
        " manifest-verified flow: export-review -> validate-manifest -> move-to-review"
        " (dry-run first). Nothing is ever deleted.",
    ]
