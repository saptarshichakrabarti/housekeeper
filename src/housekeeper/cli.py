import argparse
import sqlite3
import sys
from pathlib import Path

from .analysers.exact_duplicates import run_exact_duplicate_analysis
from .config import config_fingerprint, load_config
from .database import Database
from .jobs import tracked_job
from .logging_utils import configure_logging
from .manifests import (
    export_decision_manifest,
    export_review_manifest,
    load_manifest,
    validate_manifest_against_database,
    validate_manifest_schema,
)
from .policies import classify_all_entries
from .reporting import generate_all_reports, generate_report
from .restore import restore_transaction
from .review.decisions import create_session, export_snapshot, record_decision, validate_session
from .review_mover import move_approved_entries
from .scanner import DriveScanner
from .schedules import FORMATS, INSTALL_HINTS, INTERVALS, schedule_text


def _run_job(database, config, job_type: str, scope: dict, callback):
    with tracked_job(database, job_type, scope, config_fingerprint(config)) as job_id:
        return callback(job_id)


def _print_table(rows: list) -> None:
    """Render a list of sqlite3.Row values as an aligned, readable table."""
    if not rows:
        print("(no rows)")
        return
    columns = list(rows[0].keys())
    rendered = [{col: ("" if row[col] is None else str(row[col])) for col in columns} for row in rows]
    widths = {
        col: min(max(len(col), *(len(item[col]) for item in rendered)), 60) for col in columns
    }
    print("  ".join(col.ljust(widths[col]) for col in columns))
    print("  ".join("-" * widths[col] for col in columns))
    for item in rendered:
        print("  ".join(item[col][: widths[col]].ljust(widths[col]) for col in columns))
    print(f"({len(rows)} row{'s' if len(rows) != 1 else ''})")


def _print_row(row) -> None:
    if row is None:
        print("(not found)")
        return
    for key in row.keys():  # noqa: SIM118 - sqlite3.Row: `in row` tests values, not keys
        print(f"{key}: {'' if row[key] is None else row[key]}")


def _delta(digest: dict, key: str) -> str:
    """A signed delta, or "not comparable" — never a number the data does not support."""
    value = (digest.get("deltas") or {}).get(key)
    return "not comparable" if value is None else f"{value:+d}"


def _emit(value) -> None:
    """Print CLI query results in a human-readable form instead of raw Row reprs."""
    if isinstance(value, sqlite3.Row):
        _print_row(value)
    elif isinstance(value, list) and value and isinstance(value[0], sqlite3.Row):
        _print_table(value)
    elif isinstance(value, list) and not value:
        print("(no rows)")
    elif isinstance(value, dict):
        for key, inner in value.items():
            print(f"{key}: {inner}")
    else:
        print(value)


def _ctx(args):
    c = load_config(
        Path(args.config) if args.config else None,
        Path(args.workspace) if args.workspace else None,
    )
    c.workspace.mkdir(parents=True, exist_ok=True)
    configure_logging(c.workspace / c.data["workspace"]["logs_dir"])
    d = Database(c.database_path)
    if not (
        getattr(args, "command", None) == "database"
        and getattr(args, "database_command", None) == "migrate"
        and getattr(args, "dry_run", False)
    ):
        d.initialize()
    return c, d


def build_parser():
    p = argparse.ArgumentParser(prog="housekeeper")
    p.add_argument("--config")
    p.add_argument("--workspace")
    p.add_argument(
        "--verbose",
        "-v",
        action="store_true",
        help="show full tracebacks for unexpected errors",
    )
    p.add_argument(
        "--quiet",
        "-q",
        action="store_true",
        help="suppress live progress output on stderr",
    )
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init-workspace")
    q = sub.add_parser(
        "quickstart",
        help="one command: scan + analyse + classify + reports (read-only; never moves data)",
    )
    q.add_argument("source_root")
    q.add_argument(
        "--no-reports", action="store_true", help="skip static report generation"
    )
    q.add_argument(
        "--full",
        action="store_true",
        help="re-run every stage instead of reusing stages whose inputs are unchanged",
    )
    q.add_argument("--json", action="store_true", help="emit the run summary as JSON")
    s = sub.add_parser("scan")
    s.add_argument("source_root")
    s.add_argument("--no-resume", action="store_true")
    s.add_argument("--incremental", action="store_true", default=False)
    s.add_argument("--force-rehash", action="store_true")
    sub.add_parser("scan-status")
    sub.add_parser("stats")
    diff = sub.add_parser("diff")
    diff.add_argument("scan_a", type=int)
    diff.add_argument("scan_b", type=int)
    ch = sub.add_parser(
        "changes", help="what changed between the newest scan and the one before it"
    )
    ch.add_argument("--json", action="store_true")
    sched = sub.add_parser(
        "schedule",
        help="print a scheduler unit that runs quickstart periodically (nothing is installed)",
    )
    sched.add_argument("source_root")
    sched.add_argument("--interval", choices=list(INTERVALS), default="weekly")
    # `--weekly` reads better than `--interval weekly` and is what the documentation shows, so both
    # spellings work; they share one destination, and a later flag wins.
    every = sched.add_mutually_exclusive_group()
    for interval in INTERVALS:
        every.add_argument(
            f"--{interval}", dest="interval", action="store_const", const=interval,
            help=f"shorthand for --interval {interval}",
        )
    sched.add_argument("--format", dest="output_format", choices=list(FORMATS), default="systemd")
    a = sub.add_parser("analyse")
    a.add_argument(
        "kind",
        choices=[
            "exact-duplicates",
            "directory-overlap",
            "archives",
            "documents",
            "document-versions",
            "images",
            "contact-sheets",
            "media",
            "projects",
            "backup-lineage",
            "normalized-content",
            "image-equivalence",
            "office-equivalence",
            "pdf-equivalence",
            "archive-equivalence",
            "binary-similarity",
            "chunks",
            "chunk-overlap",
            "document-minhash",
            "backup-value",
            "preservation-risk",
            "record-series",
            "review-priority",
            "lifecycle",
            "photo-events",
            "work-sessions",
            "acquisition-batches",
            "archive-of-directory",
            "derivations",
            "all",
        ],
    )
    a.add_argument("--under")
    a.add_argument("--changed-only", action="store_true")
    a.add_argument("--source", type=int)
    a.add_argument("--scan-run", type=int)
    a.add_argument("--mime")
    a.add_argument("--extension", action="append", default=[])
    a.add_argument("--size-min", type=int)
    a.add_argument("--size-max", type=int)
    a.add_argument("--older-than")
    a.add_argument("--newer-than")
    a.add_argument("--classification")
    duplicate_scope = a.add_mutually_exclusive_group()
    duplicate_scope.add_argument("--only-unique", action="store_true")
    duplicate_scope.add_argument("--only-duplicate-candidates", action="store_true")
    a.add_argument("--content-object-ids", type=Path)
    sub.add_parser("classify")
    r = sub.add_parser("report")
    r.add_argument(
        "kind",
        choices=[
            "summary",
            "changes",
            "coverage",
            "inventory",
            "exact-duplicates",
            "directory-overlap",
            "document-versions",
            "images",
            "projects",
            "errors",
            "all",
        ],
    )
    e = sub.add_parser("export-review")
    e.add_argument("--output", required=True)
    e.add_argument("--classifications", default="REVIEW_SAFE,REVIEW_PROBABLE")
    e.add_argument("--dry-run", action="store_true")
    v = sub.add_parser("validate-manifest")
    v.add_argument("manifest")
    verify = sub.add_parser("verify-review")
    verify.add_argument("manifest")
    m = sub.add_parser("move-to-review")
    m.add_argument("manifest")
    m.add_argument("review_root")
    m.add_argument("--dry-run", action="store_true")
    m.add_argument("--yes", action="store_true")
    rr = sub.add_parser("restore")
    rr.add_argument("transaction_manifest")
    rr.add_argument("--dry-run", action="store_true")
    rr.add_argument("--yes", action="store_true")
    cov = sub.add_parser(
        "coverage",
        help="which of a source's files are verified present on another source (read-only)",
    )
    cov.add_argument("source_id", type=int)
    cov.add_argument(
        "--against",
        type=int,
        action="append",
        default=[],
        help="restrict the comparison to these source ids (default: every other source)",
    )
    cov.add_argument("--json", action="store_true")
    sources = sub.add_parser("sources")
    sources_sub = sources.add_subparsers(dest="sources_command", required=True)
    sources_sub.add_parser("list")
    ss = sources_sub.add_parser("show")
    ss.add_argument("source_id", type=int)
    sa = sources_sub.add_parser("associate")
    sa.add_argument("source_id", type=int)
    sa.add_argument("mount_path")
    jobs = sub.add_parser("jobs")
    jobs_sub = jobs.add_subparsers(dest="jobs_command", required=True)
    jobs_sub.add_parser("list")
    js = jobs_sub.add_parser("show")
    js.add_argument("job_id", type=int)
    jc = jobs_sub.add_parser("cancel")
    jc.add_argument("job_id", type=int)
    jp = jobs_sub.add_parser("pause")
    jp.add_argument("job_id", type=int)
    jr = jobs_sub.add_parser("resume")
    jr.add_argument("job_id", type=int)
    dbp = sub.add_parser("database")
    db_sub = dbp.add_subparsers(dest="database_command", required=True)
    db_sub.add_parser("stats")
    db_sub.add_parser("integrity-check")
    db_sub.add_parser("optimize")
    checkpoint = db_sub.add_parser("checkpoint")
    checkpoint.add_argument(
        "--mode", choices=["PASSIVE", "FULL", "RESTART", "TRUNCATE"], default="PASSIVE"
    )
    vacuum = db_sub.add_parser("vacuum")
    vacuum.add_argument("--yes", action="store_true")
    dbex = db_sub.add_parser("explain")
    dbex.add_argument("query_name", choices=["review_queue", "overview", "graph", "duplicates"])
    dbm = db_sub.add_parser("migrate")
    dbm.add_argument("--dry-run", action="store_true")
    dbb = db_sub.add_parser("backup")
    dbb.add_argument("output")
    dbp_prune = db_sub.add_parser(
        "prune-snapshots",
        help="bound retained scan history; prints a plan and changes nothing without --yes",
    )
    dbp_prune.add_argument(
        "--keep-per-source",
        type=int,
        default=3,
        help="most recent complete scans to keep per source root (default 3)",
    )
    dbp_prune.add_argument("--yes", action="store_true", help="actually delete the listed snapshots")
    dbpurge = db_sub.add_parser(
        "purge",
        help="delete every recorded run, all derived analysis and all generated reports (--yes)",
    )
    dbpurge.add_argument("--yes", action="store_true", help="confirm; nothing happens without it")
    review = sub.add_parser("review")
    review_sub = review.add_subparsers(dest="review_command", required=True)
    rc = review_sub.add_parser("create")
    rc.add_argument("name")
    rc.add_argument("--description", default="")
    review_sub.add_parser("list")
    rs = review_sub.add_parser("show")
    rs.add_argument("session_id", type=int)
    rd = review_sub.add_parser("decision")
    rd.add_argument("session_id", type=int)
    rd.add_argument("target_type")
    rd.add_argument("target_id", type=int)
    rd.add_argument("decision")
    rexp = review_sub.add_parser("export")
    rexp.add_argument("session_id", type=int)
    rexp.add_argument("--output", required=True)
    rexp.add_argument("--format", choices=["csv", "jsonl"], default="jsonl")
    rv = review_sub.add_parser("validate")
    rv.add_argument("session_id", type=int)
    rl = review_sub.add_parser("lock")
    rl.add_argument("session_id", type=int)
    rcan = review_sub.add_parser("canonical")
    rcan.add_argument("session_id", type=int)
    rcan.add_argument("group_id", type=int)
    rcan.add_argument("entry_id", type=int)
    chunks = sub.add_parser("chunks")
    chunks_sub = chunks.add_subparsers(dest="chunks_command", required=True)
    ce = chunks_sub.add_parser("estimate")
    ce.add_argument("--profile", default=None)
    cc = chunks_sub.add_parser("clear")
    cc.add_argument("--profile", default=None)
    cc.add_argument("--dry-run", action="store_true")
    derived = sub.add_parser("derived-data")
    derived_sub = derived.add_subparsers(dest="derived_command", required=True)
    derived_sub.add_parser("estimate")
    dclear = derived_sub.add_parser("clear")
    dclear.add_argument("kind", choices=["CHUNK_INDEX", "MINHASH_INDEX"])
    dclear.add_argument("--dry-run", action="store_true")
    collections = sub.add_parser("collections")
    collections_sub = collections.add_subparsers(dest="collections_command", required=True)
    collections_sub.add_parser("list")
    colshow = collections_sub.add_parser("show")
    colshow.add_argument("collection_id", type=int)
    colsim = collections_sub.add_parser("simulate-removal")
    colsim.add_argument("collection_id", type=int)
    colassign = collections_sub.add_parser("assign-series")
    colassign.add_argument("collection_id", type=int)
    colassign.add_argument("series_id", type=int)
    collections_sub.add_parser("retention")
    preservation = sub.add_parser("preservation")
    preservation_sub = preservation.add_subparsers(dest="preservation_command", required=True)
    preservation_sub.add_parser("queue")
    preservation_sub.add_parser("report")
    learning = sub.add_parser("learning")
    learning_sub = learning.add_subparsers(dest="learning_command", required=True)
    learning_sub.add_parser("train")
    learning_sub.add_parser("evaluate")
    learning_sub.add_parser("predict")
    learning_sub.add_parser("disable")
    known = sub.add_parser("known")
    known_sub = known.add_subparsers(dest="known_command", required=True)
    known_sub.add_parser("list")
    ka = known_sub.add_parser("assert")
    ka.add_argument("assertion")
    ka.add_argument("scope_type")
    ka.add_argument("scope_value")
    canonical = sub.add_parser("canonical")
    canonical_sub = canonical.add_subparsers(dest="canonical_command", required=True)
    canonical_sub.add_parser("assign")
    canonical_sub.add_parser("list")
    cshow = canonical_sub.add_parser("show")
    cshow.add_argument("group_id", type=int)
    cshow.add_argument("--type", default="EXACT_DUPLICATE_GROUP")
    cexplain = canonical_sub.add_parser("explain")
    cexplain.add_argument("group_id", type=int)
    cexplain.add_argument("--type", default="PIXEL_IDENTICAL_GROUP")
    dashboard = sub.add_parser("dashboard")
    dashboard.add_argument("--host", default=None)
    dashboard.add_argument("--port", type=int, default=None)
    dashboard.add_argument("--no-open-browser", action="store_true")
    dashboard.add_argument("--read-only", action="store_true")
    gui = sub.add_parser(
        "gui", help="operational dashboard: pick a directory and drive the pipeline from buttons"
    )
    gui.add_argument("--host", default=None)
    gui.add_argument("--port", type=int, default=None)
    gui.add_argument("--no-open-browser", action="store_true")
    gui.add_argument("--read-only", action="store_true")
    app_cmd = sub.add_parser(
        "app", help="native desktop window with a real OS folder picker (needs the desktop extra)"
    )
    app_cmd.add_argument("--read-only", action="store_true")
    graph = sub.add_parser("graph")
    graph_sub = graph.add_subparsers(dest="graph_command", required=True)
    gb = graph_sub.add_parser("build")
    gb.add_argument("projection", default="universe", nargs="?")
    # Unset means the configured graph.default_max_* rather than a duplicate literal here.
    gb.add_argument("--max-nodes", type=int, default=None)
    gb.add_argument("--max-edges", type=int, default=None)
    ge = graph_sub.add_parser("export")
    ge.add_argument("projection", default="universe", nargs="?")
    ge.add_argument("--output", required=True)
    ge.add_argument("--format", choices=["json", "svg"], default="json")
    ge.add_argument("--max-nodes", type=int, default=None)
    ge.add_argument("--max-edges", type=int, default=None)
    graph_sub.add_parser("cache-clear")
    benchmark = sub.add_parser("benchmark")
    benchmark.add_argument(
        "kind",
        choices=[
            "scan",
            "hashing",
            "database",
            "overlap",
            "dashboard",
            "graph",
            "baseline",
            "compare",
        ],
    )
    benchmark.add_argument("fixture", nargs="?")
    benchmark.add_argument(
        "--baseline",
        default=None,
        help="baseline artifact path for 'baseline'/'compare' (default: benchmarks/baseline.json)",
    )
    benchmark.add_argument(
        "--tolerance",
        type=float,
        default=0.5,
        help="relative wall-clock regression tolerance for 'compare' (same-runner only)",
    )
    profile = sub.add_parser("profile")
    profile.add_argument("job_id", type=int)
    acceleration = sub.add_parser("acceleration")
    acceleration_sub = acceleration.add_subparsers(dest="acceleration_command", required=True)
    acceleration_sub.add_parser("capabilities")
    ah = acceleration_sub.add_parser("hash")
    ah.add_argument("path")
    ah.add_argument("--algorithm", default="sha256")
    return p


def main(argv=None) -> int:
    """Parse arguments, then dispatch with a top-level user-facing error guard."""
    args = build_parser().parse_args(argv)
    verbose = bool(getattr(args, "verbose", False))
    database = None
    try:
        c, d = _ctx(args)
        database = d
        return _dispatch(args, c, d)
    except KeyboardInterrupt:
        print("Interrupted by user (SIGINT); no partial move was left unverified.", file=sys.stderr)
        return 130
    except BrokenPipeError:
        return 0
    except SystemExit:
        raise
    except Exception as exc:
        if verbose:
            raise
        print(f"error: {exc}", file=sys.stderr)
        return 1
    finally:
        if database is not None:
            database.close()


def _dispatch(args, c, d) -> int:
    cmd = args.command
    if cmd == "init-workspace":
        print(f"Workspace ready: {c.workspace}")
        return 0
    if cmd == "quickstart":
        import json as _json

        from .progress_line import ProgressReporter
        from .quickstart import next_steps, run_quickstart

        stage_ref: dict = {}

        def _note_stage(_message: str, stage: int, stage_total: int) -> None:
            stage_ref["stage"], stage_ref["total"] = stage, stage_total

        with ProgressReporter(c.database_path, quiet=args.quiet or args.json, stage_ref=stage_ref):
            summary = run_quickstart(
                d,
                c,
                Path(args.source_root),
                generate_reports=not args.no_reports,
                progress=_note_stage,
                full=args.full,
            )
        if args.json:
            print(_json.dumps(summary, indent=2, sort_keys=True))
            return 0
        totals = summary["totals"]
        print("\nQuickstart complete (read-only — nothing was moved or deleted).")
        print(f"  workspace:         {summary['workspace']}")
        changed = summary.get("changed_entries")
        print(
            f"  mode:              {summary.get('mode', 'full')}"
            + ("" if changed is None else f" ({changed} changed entries)")
        )
        for entry in summary["steps"]:
            result = entry["result"]
            skipped = result.get("skipped_stage") if isinstance(result, dict) else None
            if skipped:
                print(f"  skipped:           {entry['step']} ({skipped})")
        print(f"  files:             {totals['files']}")
        print(f"  directories:       {totals['directories']}")
        print(f"  content objects:   {totals['content_objects']}")
        print(f"  duplicate groups:  {totals['exact_duplicate_groups']}")
        print(f"  duplicate files:   {totals['duplicate_files']}")
        print(f"  protected:         {totals['protected']}")
        if summary.get("reports"):
            print(f"  reports written:   {len(summary['reports'])}")
        digest = summary.get("changes") or {}
        print("\nSince the last scan:")
        if digest.get("unavailable"):
            print(f"  {digest['unavailable']}")
        else:
            counts = ", ".join(
                f"{status.lower().replace('_', ' ')}: {bucket['count']}"
                for status, bucket in digest.get("buckets", {}).items()
            )
            print(f"  {counts or 'nothing changed'}")
            print(f"  duplicate groups:  {_delta(digest, 'duplicate_groups')}")
            print(f"  reviewable bytes:  {_delta(digest, 'reviewable_bytes')} (estimate)")
            if digest.get("duplicate_note"):
                print(f"  note:              {digest['duplicate_note']}")
        print("\nNext steps:")
        for step in next_steps(c):
            print(f"  - {step}")
        return 0
    if cmd == "scan":
        from .progress_line import ProgressReporter

        with ProgressReporter(c.database_path, quiet=args.quiet):
            result = DriveScanner(d, c).scan(
                Path(args.source_root),
                not args.no_resume,
                args.incremental or not args.no_resume,
                args.force_rehash,
            )
        print(result)
        return 0
    if cmd == "analyse":
        from .analysers.scope import AnalyserScope, current_inventory_runs

        scoped_content_ids = (
            frozenset(
                int(line.strip())
                for line in args.content_object_ids.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
            if args.content_object_ids
            else frozenset()
        )
        # Standalone analysis runs over the current inventory (the latest COMPLETE scan of each
        # source) rather than accumulated history, so a re-scan never groups a file with its own
        # snapshot. An explicit --scan-run means the user is targeting exactly that run.
        analyser_scope = AnalyserScope(
            scan_run_ids=(
                frozenset({int(args.scan_run)})
                if args.scan_run is not None
                else current_inventory_runs(d)
            ),
            under=args.under,
            source_id=args.source,
            detected_mime=args.mime,
            extensions=frozenset(
                value if value.startswith(".") else f".{value}" for value in args.extension
            ),
            size_min=args.size_min,
            size_max=args.size_max,
            older_than=args.older_than,
            newer_than=args.newer_than,
            classification=args.classification,
            only_unique=args.only_unique,
            only_duplicate_candidates=args.only_duplicate_candidates,
            content_object_ids=scoped_content_ids,
        )
        scope_payload = {
            **analyser_scope.__dict__,
            "scan_run_ids": sorted(analyser_scope.scan_run_ids),
            "extensions": sorted(analyser_scope.extensions),
            "content_object_ids": sorted(analyser_scope.content_object_ids),
        }
        if args.kind in {"exact-duplicates", "all"}:
            _run_job(
                d,
                c,
                "FULL_HASH",
                {"analyser": "exact-duplicates", "scope": scope_payload},
                lambda _job: run_exact_duplicate_analysis(d, c, _job, analyser_scope),
            )
        if args.kind in {"archives", "documents", "images", "media", "all"}:
            from .analysers.registry import run_content_analysis

            print(
                _run_job(
                    d,
                    c,
                    "CONTENT_ANALYSIS",
                    {"analyser": args.kind, "scope": scope_payload},
                    lambda _job: run_content_analysis(
                        d,
                        c,
                        None if args.kind == "all" else args.kind,
                        # The same scope object every other analyser on this command gets. It used
                        # to receive args.scan_run — normally None — while the CLI had already
                        # resolved the current inventory a few lines above and handed it to the
                        # duplicate and overlap stages.
                        analyser_scope,
                        args.changed_only,
                        _job,
                    ),
                )
            )
        if args.kind in {"directory-overlap", "all"}:
            from .analysers.directory_overlap import run_directory_overlap_analysis

            _run_job(
                d,
                c,
                "DIRECTORY_OVERLAP",
                {"scope": scope_payload},
                lambda _job: run_directory_overlap_analysis(d, c, analyser_scope, _job),
            )
        if args.kind in {"document-versions", "all"}:
            from .analysers.document_versions import run_document_version_analysis

            _run_job(
                d,
                c,
                "VERSION_ANALYSIS",
                {"scope": scope_payload},
                lambda _job: run_document_version_analysis(d, c, analyser_scope, _job),
            )
        if args.kind in {"projects", "all"}:
            from .analysers.projects import run_project_analysis

            _run_job(
                d,
                c,
                "PROJECT_ANALYSIS",
                {"scope": scope_payload},
                lambda _job: run_project_analysis(d, c, analyser_scope, _job),
            )
        if args.kind in {"backup-lineage", "all"}:
            from .analysers.backup_lineage import run_backup_lineage_analysis

            _run_job(
                d,
                c,
                "DIRECTORY_SUMMARY",
                {"analyser": "backup-lineage", "scope": scope_payload},
                lambda _job: run_backup_lineage_analysis(d, c, analyser_scope, _job),
            )
        if args.kind in {"images", "all"}:
            from .analysers.images import run_image_analysis

            _run_job(
                d,
                c,
                "IMAGE_ANALYSIS",
                {"analyser": "similarity", "scope": scope_payload},
                lambda _job: run_image_analysis(d, c, analyser_scope, _job),
            )
        if args.kind in {"contact-sheets", "all"}:
            # Contact sheets composite existing thumbnails of each IMAGE_SIMILARITY group; they run
            # after image similarity so the groups exist.
            from .analysers.contact_sheets import run_contact_sheet_generation

            result = _run_job(
                d,
                c,
                "CONTACT_SHEET_GENERATION",
                {"analyser": "contact-sheets", "scope": scope_payload},
                lambda _job: run_contact_sheet_generation(d, c, analyser_scope, _job),
            )
            if args.kind == "contact-sheets":
                print(result)
        if args.kind in {
            "normalized-content",
            "image-equivalence",
            "office-equivalence",
            "pdf-equivalence",
            "archive-equivalence",
        }:
            from .analysers.normalized_content import run_normalized_content_analysis
            from .canonical.roles import assign_canonical_roles

            result = _run_job(
                d,
                c,
                "CONTENT_ANALYSIS",
                {"analyser": args.kind, "scope": scope_payload},
                lambda _job: run_normalized_content_analysis(d, c, analyser_scope, _job),
            )
            assign_canonical_roles(d)
            print(result)
        if args.kind == "chunks":
            from .analysers.content_defined_chunks import run_chunk_analysis

            print(
                _run_job(
                    d,
                    c,
                    "CHUNK_ANALYSIS",
                    {"scope": scope_payload},
                    lambda _job: run_chunk_analysis(d, c, analyser_scope, _job),
                )
            )
        if args.kind == "chunk-overlap":
            from .analysers.content_defined_chunks import run_chunk_overlap_analysis

            print(
                _run_job(
                    d, c, "CHUNK_OVERLAP", {}, lambda _job: run_chunk_overlap_analysis(d, c, _job)
                )
            )
        if args.kind == "binary-similarity":
            from .analysers.binary_similarity import run_binary_similarity_analysis

            print(
                _run_job(d, c, "CONTENT_ANALYSIS", {"analyser": "binary-similarity"},
                         lambda _job: run_binary_similarity_analysis(d, c, _job))
            )
        if args.kind == "document-minhash":
            from .analysers.document_minhash import run_document_minhash_analysis

            print(
                _run_job(
                    d,
                    c,
                    "VERSION_ANALYSIS",
                    {"scope": scope_payload},
                    lambda _job: run_document_minhash_analysis(d, c, analyser_scope, _job),
                )
            )
        if args.kind == "backup-value":
            from .collections.marginal_value import run_backup_value_analysis

            print(_run_job(d, c, "DIRECTORY_SUMMARY", {}, lambda _job: run_backup_value_analysis(d, c, job_id=_job)))
        if args.kind == "preservation-risk":
            from .analysers.preservation_risk import run_preservation_risk_analysis

            print(
                _run_job(
                    d, c, "PROJECT_ANALYSIS", {}, lambda _job: run_preservation_risk_analysis(d, c, job_id=_job)
                )
            )
        if args.kind == "record-series":
            from .collections.record_series import run_record_series_analysis

            print(_run_job(d, c, "CLASSIFICATION", {}, lambda _job: run_record_series_analysis(d, c, job_id=_job)))
        if args.kind == "review-priority":
            from .analysers.review_priority import run_review_priority_analysis

            print(
                _run_job(d, c, "CLASSIFICATION", {}, lambda _job: run_review_priority_analysis(d, c, job_id=_job))
            )
        if args.kind == "lifecycle":
            from .analysers.lifecycle import run_lifecycle_analysis

            print(_run_job(d, c, "CLASSIFICATION", {}, lambda _job: run_lifecycle_analysis(d, c, job_id=_job)))
        if args.kind == "photo-events":
            from .collections.events import run_photo_event_analysis

            print(_run_job(d, c, "IMAGE_ANALYSIS", {}, lambda _job: run_photo_event_analysis(d, c, job_id=_job)))
        if args.kind == "work-sessions":
            from .collections.events import run_work_session_analysis

            print(_run_job(d, c, "VERSION_ANALYSIS", {}, lambda _job: run_work_session_analysis(d, c, job_id=_job)))
        if args.kind == "acquisition-batches":
            from .collections.events import run_acquisition_batch_analysis

            print(
                _run_job(d, c, "DIRECTORY_SUMMARY", {}, lambda _job: run_acquisition_batch_analysis(d, c, job_id=_job))
            )
        if args.kind == "archive-of-directory":
            from .analysers.archive_equivalence import run_archive_directory_analysis

            print(
                _run_job(d, c, "ARCHIVE_ANALYSIS", {}, lambda _job: run_archive_directory_analysis(d, c, job_id=_job))
            )
        if args.kind == "derivations":
            from .analysers.cross_format_derivation import run_cross_format_derivation_analysis

            print(
                _run_job(
                    d,
                    c,
                    "VERSION_ANALYSIS",
                    {"scope": scope_payload},
                    lambda _job: run_cross_format_derivation_analysis(d, c, analyser_scope, _job),
                )
            )
        if args.kind == "all":
            # Include the cheap advanced analysers in `all`; chunking, MinHash, and binary fuzzy
            # similarity stay opt-in (large/expensive). Priority + lifecycle run after classify.
            from .analysers.archive_equivalence import run_archive_directory_analysis
            from .analysers.cross_format_derivation import run_cross_format_derivation_analysis
            from .analysers.normalized_content import run_normalized_content_analysis
            from .analysers.preservation_risk import run_preservation_risk_analysis
            from .canonical.roles import assign_canonical_roles
            from .collections.events import run_photo_event_analysis
            from .collections.marginal_value import run_backup_value_analysis
            from .collections.record_series import run_record_series_analysis

            _run_job(d, c, "CONTENT_ANALYSIS", {"analyser": "normalized-content"},
                     lambda _job: run_normalized_content_analysis(d, c, analyser_scope, _job))
            assign_canonical_roles(d)
            _run_job(d, c, "VERSION_ANALYSIS", {}, lambda _job: run_cross_format_derivation_analysis(d, c, job_id=_job))
            _run_job(d, c, "ARCHIVE_ANALYSIS", {}, lambda _job: run_archive_directory_analysis(d, c, job_id=_job))
            _run_job(d, c, "DIRECTORY_SUMMARY", {}, lambda _job: run_backup_value_analysis(d, c, job_id=_job))
            _run_job(d, c, "CLASSIFICATION", {}, lambda _job: run_record_series_analysis(d, c, job_id=_job))
            _run_job(d, c, "PROJECT_ANALYSIS", {}, lambda _job: run_preservation_risk_analysis(d, c, job_id=_job))
            _run_job(d, c, "IMAGE_ANALYSIS", {}, lambda _job: run_photo_event_analysis(d, c, job_id=_job))
        return 0
    if cmd == "classify":
        from .analysers.lifecycle import run_lifecycle_analysis
        from .analysers.review_priority import run_review_priority_analysis

        _run_job(d, c, "CLASSIFICATION", {}, lambda _job: classify_all_entries(d, c, job_id=_job))
        # Prioritization and lifecycle depend on classifications, so they run right after.
        _run_job(d, c, "CLASSIFICATION", {"stage": "review-priority"},
                 lambda _job: run_review_priority_analysis(d, c, job_id=_job))
        _run_job(d, c, "CLASSIFICATION", {"stage": "lifecycle"},
                 lambda _job: run_lifecycle_analysis(d, c, job_id=_job))
        return 0
    if cmd == "report":
        print(
            _run_job(
                d,
                c,
                "REPORT_GENERATION",
                {"kind": args.kind},
                lambda _job: [
                    p.as_posix()
                    for p in (
                        generate_all_reports(d, c, job_id=_job)
                        if args.kind == "all"
                        else [generate_report(args.kind, d, c)]
                    )
                ],
            )
        )
        return 0
    if cmd == "export-review":
        _run_job(
            d,
            c,
            "MANIFEST_EXPORT",
            {"output": args.output},
            lambda _job: export_review_manifest(
                d, Path(args.output), set(args.classifications.split(","))
            ),
        )
        return 0
    if cmd == "validate-manifest":
        errors = _run_job(
            d,
            c,
            "MANIFEST_VALIDATION",
            {"manifest": args.manifest},
            lambda _job: (
                validate_manifest_schema(load_manifest(Path(args.manifest)))
                + validate_manifest_against_database(load_manifest(Path(args.manifest)), d)
            ),
        )
        print("valid" if not errors else "\n".join(errors))
        return 0 if not errors else 2
    if cmd == "verify-review":
        from .restore import verify_transaction

        results = _run_job(
            d,
            c,
            "MANIFEST_VALIDATION",
            {"transaction": args.manifest},
            lambda _job: verify_transaction(Path(args.manifest)),
        )
        for record in results:
            print(f"{record.get('verify_status')}: {record.get('destination_path')}")
        unverified = [r for r in results if r.get("verify_status") not in {"VERIFIED", "SKIPPED"}]
        print(f"{len(results) - len(unverified)}/{len(results)} verified")
        return 0 if not unverified else 2
    if cmd == "move-to-review":
        entries = load_manifest(Path(args.manifest))
        errors = validate_manifest_schema(entries) + validate_manifest_against_database(entries, d)
        if errors:
            raise SystemExit("refusing invalid manifest: " + "; ".join(errors))
        tx = _run_job(
            d,
            c,
            "REVIEW_MOVE",
            {"manifest": args.manifest, "dry_run": args.dry_run},
            lambda _job: move_approved_entries(
                entries, Path(args.review_root), d, args.dry_run, args.yes, Path(args.manifest)
            ),
        )
        print(tx)
        return 0
    if cmd == "restore":
        print(
            _run_job(
                d,
                c,
                "RESTORE",
                {"transaction": args.transaction_manifest, "dry_run": args.dry_run},
                lambda _job: restore_transaction(
                    Path(args.transaction_manifest), args.dry_run, args.yes
                ),
            )
        )
        return 0
    if cmd == "scan-status":
        _emit(
            d.fetch_all(
                "SELECT id,source_root,status,files_seen,directories_seen,symlinks_seen,errors_seen,bytes_seen,started_at,completed_at FROM scan_runs ORDER BY id DESC LIMIT 5"
            )
        )
        return 0
    if cmd == "stats":
        _emit(
            d.fetch_all(
                "SELECT entry_type,COUNT(*) n,COALESCE(SUM(size_bytes),0) bytes FROM filesystem_entries GROUP BY entry_type ORDER BY entry_type"
            )
        )
        return 0
    if cmd == "changes":
        import json as _json

        from .reports.contexts import changes_digest

        digest = changes_digest(d, c)
        if args.json:
            print(_json.dumps(digest, indent=2, sort_keys=True, default=str))
            return 0
        if digest.get("unavailable"):
            print(digest["unavailable"])
            return 0
        for status, bucket in digest["buckets"].items():
            print(f"{status.lower().replace('_', ' ')}: {bucket['count']} ({bucket['bytes']} bytes)")
        print(f"duplicate groups: {_delta(digest, 'duplicate_groups')}")
        print(f"reviewable bytes: {_delta(digest, 'reviewable_bytes')} (estimate)")
        if digest.get("duplicate_note"):
            print(digest["duplicate_note"])
        return 0
    if cmd == "schedule":
        # Printed, never installed: a scheduler unit belongs to the operator, and this tool does not
        # write into ~/.config/systemd or the task store on their behalf.
        for name, text in schedule_text(
            c, Path(args.source_root), args.interval, args.output_format
        ).items():
            if name:
                print(f"# ---- {name}")
            print(text, end="")
        print(f"\n# {INSTALL_HINTS[args.output_format]}")
        return 0
    if cmd == "diff":
        _emit(
            d.fetch_all(
                "SELECT change_status,COUNT(*) n FROM scan_entry_changes WHERE scan_run_id=? GROUP BY change_status ORDER BY change_status",
                (args.scan_b,),
            )
        )
        _emit(
            d.fetch_all(
                "SELECT relative_path,change_status FROM scan_entry_changes WHERE scan_run_id=? ORDER BY relative_path LIMIT 200",
                (args.scan_b,),
            )
        )
        return 0
    if cmd == "coverage":
        import json as _json

        from .coverage import coverage

        result = coverage(d, args.source_id, args.against or None)
        if args.json:
            print(_json.dumps(result, indent=2, sort_keys=True))
            return 0
        print(result["summary"])
        for name, bucket in result["buckets"].items():
            print(f"  {name:<8} {bucket['count']:>10,} files  {bucket['bytes']:>16,} bytes")
        if result["unique_files"]:
            print("\nLargest files with no verified copy on another source:")
            for entry in result["unique_files"][:20]:
                print(f"  {entry['size_bytes']:>16,}  {entry['relative_path']}")
        print(
            "\n'verified elsewhere' means the same content was found on another source — not that "
            "anything is safe to delete. Moving files stays the separate export-review -> "
            "validate-manifest -> move-to-review flow."
        )
        return 0
    if cmd == "sources":
        if args.sources_command == "list":
            _emit(
                d.fetch_all(
                    "SELECT id,display_name,last_mount_path,last_seen_at FROM source_roots ORDER BY id"
                )
            )
        elif args.sources_command == "show":
            _emit(d.fetch_one("SELECT * FROM source_roots WHERE id=?", (args.source_id,)))
        else:
            d.connect().execute(
                "UPDATE source_roots SET last_mount_path=?,last_seen_at=CURRENT_TIMESTAMP WHERE id=?",
                (str(Path(args.mount_path).resolve()), args.source_id),
            )
            d.connect().commit()
            print("associated")
        return 0
    if cmd == "jobs":
        if args.jobs_command == "pause":
            from .jobs import request_pause

            request_pause(d, args.job_id)
            print("pause requested")
            return 0
        if args.jobs_command == "cancel":
            from .jobs import request_cancel

            request_cancel(d, args.job_id)
            print("cancellation requested")
            return 0
        if args.jobs_command == "resume":
            import json

            from .jobs import resume_job

            resume_job(d, args.job_id)
            job = d.fetch_one("SELECT job_type,scope_json FROM jobs WHERE id=?", (args.job_id,))
            if job and job["job_type"] == "SCAN":
                scope = json.loads(job["scope_json"] or "{}")
                source = scope.get("source_root")
                if not source:
                    raise SystemExit("scan job has no resumable source root")
                print(
                    DriveScanner(d, c).scan(
                        Path(source),
                        incremental=bool(scope.get("incremental", True)),
                        job_id=args.job_id,
                    )
                )
            else:
                print(
                    "resume scheduled; rerun the corresponding analyser command to continue its idempotent content work"
                )
            return 0
        if args.jobs_command == "show":
            _emit(d.fetch_one("SELECT * FROM jobs WHERE id=?", (args.job_id,)))
        else:
            _emit(
                d.fetch_all(
                    "SELECT id,job_type,status,processed_count,total_estimate,error_count,updated_at FROM jobs ORDER BY id DESC LIMIT 50"
                )
            )
        return 0
    if cmd == "database":
        if args.database_command == "backup":
            print(
                _run_job(
                    d,
                    c,
                    "DATABASE_BACKUP",
                    {"output": args.output},
                    lambda _job: d.backup(Path(args.output)),
                )
            )
        elif args.database_command == "integrity-check":
            print(
                _run_job(
                    d,
                    c,
                    "DATABASE_MAINTENANCE",
                    {"operation": "integrity-check"},
                    lambda _job: d.integrity_check(),
                )
            )
        elif args.database_command == "optimize":
            _run_job(
                d,
                c,
                "DATABASE_MAINTENANCE",
                {"operation": "optimize"},
                lambda _job: (d.connect().execute("PRAGMA optimize"), d.connect().commit()),
            )
            print("optimized")
        elif args.database_command == "checkpoint":
            print(
                _run_job(
                    d,
                    c,
                    "DATABASE_MAINTENANCE",
                    {"operation": "checkpoint", "mode": args.mode},
                    lambda _job: {"wal_checkpoint": d.checkpoint_wal(args.mode)},
                )
            )
        elif args.database_command == "prune-snapshots":
            # Dry run by default: this is the only command that deletes recorded history, and the
            # plan is the interesting output either way — it names what is held and why.
            if not args.yes:
                plan = d.snapshot_retention_plan(args.keep_per_source)
                plan["dry_run"] = True
                _emit(plan)
            else:
                _emit(
                    _run_job(
                        d,
                        c,
                        "DATABASE_MAINTENANCE",
                        {"operation": "prune-snapshots", "keep": args.keep_per_source},
                        lambda _job: d.prune_snapshots(args.keep_per_source),
                    )
                )
        elif args.database_command == "purge":
            # Not wrapped in _run_job: the purge deletes the ``jobs`` table the job row lives in.
            if not args.yes:
                raise SystemExit(
                    "purge deletes every recorded run, all derived analysis and all generated "
                    "reports (the source drive is untouched); rerun with --yes"
                )
            from .database_maintenance import purge_runs

            _emit(purge_runs(d, c))
        elif args.database_command == "vacuum":
            if not args.yes:
                raise SystemExit(
                    "VACUUM can require a full temporary database copy; rerun with --yes"
                )
            _run_job(d, c, "DATABASE_MAINTENANCE", {"operation": "vacuum"}, lambda _job: d.vacuum())
            print("vacuumed")
        elif args.database_command == "explain":
            queries = {
                "review_queue": "SELECT e.id FROM filesystem_entries e LEFT JOIN classifications c ON c.entry_id=e.id WHERE e.id>? ORDER BY e.id LIMIT ?",
                "overview": "SELECT classification,COUNT(*) FROM classifications GROUP BY classification",
                "graph": "SELECT * FROM relationships WHERE confidence>=? ORDER BY confidence DESC LIMIT ?",
                "duplicates": "SELECT * FROM exact_duplicate_groups ORDER BY id LIMIT ?",
            }
            params = {
                "review_queue": (0, 100),
                "overview": (),
                "graph": (0.7, 2000),
                "duplicates": (100,),
            }
            _emit(
                d.fetch_all(
                    "EXPLAIN QUERY PLAN " + queries[args.query_name], params[args.query_name]
                )
            )
        elif args.database_command == "migrate":
            if args.dry_run:
                from .migrations import migration_plan

                print(migration_plan(d))
            else:
                d.initialize()
                print(d.database_stats())
        else:
            print(d.database_stats())
        return 0
    if cmd == "review":
        if args.review_command == "create":
            print(create_session(d, args.name, args.description))
        elif args.review_command == "list":
            _emit(
                d.fetch_all(
                    "SELECT id,name,status,updated_at FROM review_sessions ORDER BY id DESC"
                )
            )
        elif args.review_command == "decision":
            print(
                record_decision(d, args.session_id, args.target_type, args.target_id, args.decision)
            )
        elif args.review_command == "export":
            import hashlib

            errors = validate_session(d, args.session_id)
            if errors:
                raise SystemExit("refusing to export invalid review session: " + "; ".join(errors))

            manifest_path = export_decision_manifest(
                d, args.session_id, Path(args.output), args.format
            )
            manifest_hash = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
            export_snapshot(
                d, args.session_id, {"manifest_hash": manifest_hash, "format": args.format}
            )
            d.connect().execute(
                "UPDATE review_sessions SET status='EXPORTED',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (args.session_id,),
            )
            d.connect().commit()
            print(manifest_path)
        elif args.review_command == "validate":
            errors = validate_session(d, args.session_id)
            print("valid" if not errors else "\n".join(errors))
            return 0 if not errors else 2
        elif args.review_command == "lock":
            d.connect().execute(
                "UPDATE review_sessions SET status='LOCKED',updated_at=CURRENT_TIMESTAMP WHERE id=?",
                (args.session_id,),
            )
            d.connect().commit()
            print("review session locked")
        elif args.review_command == "canonical":
            from .review.canonical import override_canonical

            override_canonical(d, args.session_id, args.group_id, args.entry_id)
            print("canonical override recorded")
        else:
            _emit(d.fetch_one("SELECT * FROM review_sessions WHERE id=?", (args.session_id,)))
        return 0
    if cmd == "chunks":
        from .chunking.index import clear_chunk_index, estimate_chunk_analysis

        if args.chunks_command == "estimate":
            _emit(estimate_chunk_analysis(d, c))
        else:
            _emit(clear_chunk_index(d, None, args.dry_run))
        return 0
    if cmd == "derived-data":
        from .chunking.index import clear_chunk_index, estimate_chunk_analysis

        if args.derived_command == "estimate":
            _emit(estimate_chunk_analysis(d, c))
        elif args.kind == "CHUNK_INDEX":
            _emit(clear_chunk_index(d, None, args.dry_run))
        else:
            from .similarity.minhash import clear_minhash_index

            _emit(clear_minhash_index(d, args.dry_run))
        return 0
    if cmd == "collections":
        from .collections.marginal_value import simulate_removal

        if args.collections_command == "list":
            _emit(
                d.fetch_all(
                    "SELECT id,cluster_type,name,confidence FROM collection_clusters ORDER BY id DESC LIMIT 100"
                )
            )
        elif args.collections_command == "show":
            _emit(d.fetch_one("SELECT * FROM collection_clusters WHERE id=?", (args.collection_id,)))
            _emit(
                d.fetch_all(
                    "SELECT member_type,member_id,sequence_index FROM collection_members WHERE cluster_id=? ORDER BY sequence_index LIMIT 200",
                    (args.collection_id,),
                )
            )
        elif args.collections_command == "simulate-removal":
            _emit(simulate_removal(d, args.collection_id))
        elif args.collections_command == "retention":
            from .collections.retention import apply_retention_policies

            _emit(apply_retention_policies(d, c))
        else:
            from .collections.record_series import assign_series_to_collection

            assign_series_to_collection(d, args.collection_id, args.series_id)
            print("assigned")
        return 0
    if cmd == "preservation":
        if args.preservation_command == "queue":
            _emit(
                d.fetch_all(
                    """SELECT p.target_id,e.relative_path,p.recommended_action,p.format_risk,p.encryption_risk,p.integrity_risk
                       FROM preservation_assessments p JOIN filesystem_entries e ON e.id=p.target_id
                       WHERE p.recommended_action NOT IN ('KEEP_WITH_CHECKSUM') ORDER BY p.id LIMIT 200"""
                )
            )
        else:
            _emit(
                d.fetch_all(
                    "SELECT recommended_action,COUNT(*) n FROM preservation_assessments GROUP BY recommended_action ORDER BY n DESC"
                )
            )
        return 0
    if cmd == "learning":
        from .learning import prediction, training

        if args.learning_command == "train":
            _emit(training.train_model(d, c))
        elif args.learning_command == "evaluate":
            _emit(training.evaluate_model(d, c))
        elif args.learning_command == "predict":
            _emit(prediction.predict_pending(d, c))
        else:
            d.connect().execute("UPDATE review_learning_models SET active=0")
            d.connect().commit()
            print("learning disabled")
        return 0
    if cmd == "known":
        from .known_content import add_assertion, list_assertions

        if args.known_command == "list":
            _emit(list_assertions(d))
        else:
            print(add_assertion(d, args.assertion, args.scope_type, args.scope_value))
        return 0
    if cmd == "canonical":
        from .canonical.roles import assign_canonical_roles, roles_for_group

        if args.canonical_command == "assign":
            print(assign_canonical_roles(d))
        elif args.canonical_command in {"show", "explain"}:
            group_type = args.type
            _emit(roles_for_group(d, group_type, args.group_id))
        else:
            _emit(
                d.fetch_all(
                    "SELECT target_group_type,target_group_id,canonical_role,entry_id,content_object_id FROM canonical_assignments WHERE superseded_at IS NULL ORDER BY id DESC LIMIT 100"
                )
            )
        return 0
    if cmd == "benchmark":
        if args.kind in {"baseline", "compare"}:
            import json
            import tempfile

            from .benchmarking import (
                compare,
                default_baseline_path,
                load_baseline,
                run_suite,
                write_baseline,
            )

            baseline_path = Path(args.baseline) if args.baseline else default_baseline_path()
            # The suite runs against its own throwaway workspace so a benchmark never mutates the
            # user's real inventory database.
            with tempfile.TemporaryDirectory() as tmp:
                suite = run_suite(Path(tmp))
            if args.kind == "baseline":
                write_baseline(baseline_path, suite)
                print(
                    json.dumps(
                        {
                            "status": "written",
                            "path": str(baseline_path),
                            "profiles": sorted(suite["profiles"]),
                            "environment": suite["environment"],
                        }
                    )
                )
                return 0
            if not baseline_path.exists():
                print(json.dumps({"status": "no-baseline", "path": str(baseline_path)}))
                return 1
            result = compare(suite, load_baseline(baseline_path), timing_tolerance=args.tolerance)
            print(json.dumps(result, indent=2))
            # A count regression (correctness drift) or a same-runner timing regression fails.
            return 0 if result["ok"] else 1
        if args.kind == "scan" and args.fixture:
            import time

            start = time.perf_counter()
            counts = DriveScanner(d, c).scan(Path(args.fixture), incremental=False)
            print({"seconds": time.perf_counter() - start, **counts})
        else:
            print(
                {"kind": args.kind, "status": "use benchmarks/ scripts for synthetic fixture runs"}
            )
        return 0
    if cmd == "profile":
        _emit(d.fetch_one("SELECT * FROM jobs WHERE id=?", (args.job_id,)))
        return 0
    if cmd == "acceleration":
        from .acceleration.capability_detection import detect_backend

        backend = detect_backend()
        if args.acceleration_command == "capabilities":
            print(backend.capabilities())
        else:
            print(backend.full_hash(args.path, args.algorithm))
        return 0
    if cmd == "graph":
        import json

        from .graph.builder import build_projection

        if args.graph_command == "cache-clear":
            _run_job(
                d,
                c,
                "GRAPH_GENERATION",
                {"operation": "cache-clear"},
                lambda _job: (
                    d.connect().execute("DELETE FROM graph_layout_cache"),
                    d.connect().commit(),
                ),
            )
            print("graph cache cleared")
        else:
            payload = _run_job(
                d,
                c,
                "GRAPH_GENERATION",
                {
                    "projection": args.projection,
                    "max_nodes": args.max_nodes,
                    "max_edges": args.max_edges,
                },
                lambda _job: build_projection(
                    d,
                    args.projection,
                    max_nodes=args.max_nodes,
                    max_edges=args.max_edges,
                    config=c,
                ),
            )
            if args.graph_command == "export":
                if args.format == "svg":
                    from .graph.serialization import to_svg

                    content = to_svg(payload)
                else:
                    content = json.dumps(payload, indent=2)
                _run_job(
                    d,
                    c,
                    "GRAPH_EXPORT",
                    {"projection": args.projection, "output": args.output, "format": args.format},
                    lambda _job: Path(args.output).write_text(content, encoding="utf-8"),
                )
                print(args.output)
            else:
                print(json.dumps(payload, indent=2))
        return 0
    if cmd == "dashboard":
        import uvicorn

        from .analysers.contact_sheets import contact_sheet_dir
        from .dashboard.app import create_app

        host = args.host or c.data["dashboard"]["host"]
        if host not in {"127.0.0.1", "localhost", "::1"} and not c.data["dashboard"].get(
            "allow_non_loopback", False
        ):
            raise SystemExit(
                "refusing non-loopback dashboard binding; set dashboard.allow_non_loopback=true explicitly"
            )
        uvicorn.run(
            create_app(d, args.read_only, contact_sheet_dir(c)),
            host=host,
            port=args.port or c.data["dashboard"]["port"],
            log_level="info",
        )
        return 0
    if cmd == "gui":
        import threading
        import webbrowser

        import uvicorn

        from .analysers.contact_sheets import contact_sheet_dir
        from .dashboard.app import create_app

        host = args.host or c.data["dashboard"]["host"]
        if host not in {"127.0.0.1", "localhost", "::1"} and not c.data["dashboard"].get(
            "allow_non_loopback", False
        ):
            raise SystemExit(
                "refusing non-loopback dashboard binding; set dashboard.allow_non_loopback=true explicitly"
            )
        port = args.port or c.data["dashboard"]["port"]
        if not args.no_open_browser:
            threading.Timer(1.0, lambda: webbrowser.open(f"http://{host}:{port}")).start()
        uvicorn.run(
            create_app(d, args.read_only, contact_sheet_dir(c), config=c),
            host=host,
            port=port,
            log_level="info",
        )
        return 0
    if cmd == "app":
        import socket
        import threading
        import time

        import uvicorn

        from .analysers.contact_sheets import contact_sheet_dir
        from .dashboard.app import create_app
        from .desktop import Api

        host, port = "127.0.0.1", c.data["dashboard"]["port"]
        application = create_app(d, args.read_only, contact_sheet_dir(c), config=c)
        threading.Thread(
            target=lambda: uvicorn.run(application, host=host, port=port, log_level="warning"),
            daemon=True,
        ).start()
        for _ in range(50):
            try:
                with socket.create_connection((host, port), timeout=0.2):
                    break
            except OSError:
                time.sleep(0.1)
        try:
            import webview
        except ImportError:
            raise SystemExit(
                "the desktop app needs pywebview; install it with: pip install 'drive-housekeeper[desktop]'"
            )

        api = Api()
        window = webview.create_window("drive_housekeeper", f"http://{host}:{port}", js_api=api)
        api.window = window
        webview.start()
        return 0
    return 0
