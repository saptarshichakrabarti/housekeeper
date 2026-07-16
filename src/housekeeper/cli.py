import argparse
from pathlib import Path

from .analyzers.exact_duplicates import run_exact_duplicate_analysis
from .config import config_fingerprint, load_config
from .database import Database
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
from .review_mover import move_approved_entries
from .scanner import DriveScanner
from .review.decisions import create_session, export_snapshot, record_decision, validate_session
from .jobs import tracked_job


def _run_job(database, config, job_type: str, scope: dict, callback):
    with tracked_job(database, job_type, scope, config_fingerprint(config)) as job_id:
        return callback(job_id)


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
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("init-workspace")
    s = sub.add_parser("scan")
    s.add_argument("source_root")
    s.add_argument("--no-resume", action="store_true")
    s.add_argument("--incremental", action="store_true", default=False)
    s.add_argument("--changed-only", action="store_true")
    s.add_argument("--force-rehash", action="store_true")
    sub.add_parser("scan-status")
    sub.add_parser("stats")
    diff = sub.add_parser("diff")
    diff.add_argument("scan_a", type=int)
    diff.add_argument("scan_b", type=int)
    a = sub.add_parser("analyze")
    a.add_argument(
        "kind",
        choices=[
            "exact-duplicates",
            "directory-overlap",
            "archives",
            "documents",
            "document-versions",
            "images",
            "media",
            "projects",
            "backup-lineage",
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
    dashboard = sub.add_parser("dashboard")
    dashboard.add_argument("--host", default=None)
    dashboard.add_argument("--port", type=int, default=None)
    dashboard.add_argument("--no-open-browser", action="store_true")
    dashboard.add_argument("--read-only", action="store_true")
    graph = sub.add_parser("graph")
    graph_sub = graph.add_subparsers(dest="graph_command", required=True)
    gb = graph_sub.add_parser("build")
    gb.add_argument("projection", default="universe", nargs="?")
    gb.add_argument("--max-nodes", type=int, default=500)
    gb.add_argument("--max-edges", type=int, default=2000)
    ge = graph_sub.add_parser("export")
    ge.add_argument("projection", default="universe", nargs="?")
    ge.add_argument("--output", required=True)
    ge.add_argument("--max-nodes", type=int, default=500)
    ge.add_argument("--max-edges", type=int, default=2000)
    graph_sub.add_parser("cache-clear")
    benchmark = sub.add_parser("benchmark")
    benchmark.add_argument(
        "kind", choices=["scan", "hashing", "database", "overlap", "dashboard", "graph"]
    )
    benchmark.add_argument("fixture", nargs="?")
    profile = sub.add_parser("profile")
    profile.add_argument("job_id", type=int)
    acceleration = sub.add_parser("acceleration")
    acceleration_sub = acceleration.add_subparsers(dest="acceleration_command", required=True)
    acceleration_sub.add_parser("capabilities")
    ah = acceleration_sub.add_parser("hash")
    ah.add_argument("path")
    ah.add_argument("--algorithm", default="sha256")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    c, d = _ctx(args)
    cmd = args.command
    if cmd == "init-workspace":
        print(f"Workspace ready: {c.workspace}")
        return 0
    if cmd == "scan":
        print(
            DriveScanner(d, c).scan(
                Path(args.source_root),
                not args.no_resume,
                args.incremental or not args.no_resume,
                args.changed_only,
                args.force_rehash,
            )
        )
        return 0
    if cmd == "analyze":
        from .analyzers.scope import AnalyzerScope

        scoped_content_ids = (
            frozenset(
                int(line.strip())
                for line in args.content_object_ids.read_text(encoding="utf-8").splitlines()
                if line.strip()
            )
            if args.content_object_ids
            else frozenset()
        )
        analyzer_scope = AnalyzerScope(
            under=args.under,
            source_id=args.source,
            scan_run_id=args.scan_run,
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
            **analyzer_scope.__dict__,
            "extensions": sorted(analyzer_scope.extensions),
            "content_object_ids": sorted(analyzer_scope.content_object_ids),
        }
        if args.kind in {"exact-duplicates", "all"}:
            _run_job(
                d,
                c,
                "FULL_HASH",
                {"analyzer": "exact-duplicates", "scope": scope_payload},
                lambda _job: run_exact_duplicate_analysis(d, c, _job, analyzer_scope),
            )
        if args.kind in {"archives", "documents", "images", "media", "all"}:
            from .analyzers.registry import run_content_analysis

            print(
                _run_job(
                    d,
                    c,
                    "CONTENT_ANALYSIS",
                    {"analyzer": args.kind, "scope": scope_payload},
                    lambda _job: run_content_analysis(
                        d,
                        c,
                        None if args.kind == "all" else args.kind,
                        args.under,
                        args.changed_only,
                        args.source,
                        set(args.extension),
                        args.size_min,
                        args.size_max,
                        args.older_than,
                        args.newer_than,
                        args.classification,
                        set(scoped_content_ids) or None,
                        args.scan_run,
                        args.mime,
                        args.only_unique,
                        args.only_duplicate_candidates,
                        _job,
                    ),
                )
            )
        if args.kind in {"directory-overlap", "all"}:
            from .analyzers.directory_overlap import run_directory_overlap_analysis

            _run_job(
                d,
                c,
                "DIRECTORY_OVERLAP",
                {"scope": scope_payload},
                lambda _job: run_directory_overlap_analysis(d, c, analyzer_scope),
            )
        if args.kind in {"document-versions", "all"}:
            from .analyzers.document_versions import run_document_version_analysis

            _run_job(
                d,
                c,
                "VERSION_ANALYSIS",
                {"scope": scope_payload},
                lambda _job: run_document_version_analysis(d, c, analyzer_scope, _job),
            )
        if args.kind in {"projects", "all"}:
            from .analyzers.projects import run_project_analysis

            _run_job(
                d,
                c,
                "PROJECT_ANALYSIS",
                {"scope": scope_payload},
                lambda _job: run_project_analysis(d, c, analyzer_scope, _job),
            )
        if args.kind in {"backup-lineage", "all"}:
            from .analyzers.backup_lineage import run_backup_lineage_analysis

            _run_job(
                d,
                c,
                "DIRECTORY_SUMMARY",
                {"analyzer": "backup-lineage", "scope": scope_payload},
                lambda _job: run_backup_lineage_analysis(d, c, analyzer_scope, _job),
            )
        if args.kind in {"images", "all"}:
            from .analyzers.images import run_image_analysis

            _run_job(
                d,
                c,
                "IMAGE_ANALYSIS",
                {"analyzer": "similarity", "scope": scope_payload},
                lambda _job: run_image_analysis(d, c, analyzer_scope, _job),
            )
        return 0
    if cmd == "classify":
        _run_job(d, c, "CLASSIFICATION", {}, lambda _job: classify_all_entries(d, c))
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
                        generate_all_reports(d, c)
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
    if cmd in {"validate-manifest", "verify-review"}:
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
        print(
            d.fetch_all(
                "SELECT id,source_root,status,files_seen,bytes_seen FROM scan_runs ORDER BY id DESC LIMIT 1"
            )
        )
        return 0
    if cmd == "stats":
        print(
            d.fetch_all(
                "SELECT entry_type,COUNT(*) n,SUM(size_bytes) bytes FROM filesystem_entries GROUP BY entry_type"
            )
        )
        return 0
    if cmd == "diff":
        print(
            d.fetch_all(
                "SELECT relative_path,change_status,evidence_json FROM scan_entry_changes WHERE scan_run_id=? ORDER BY relative_path",
                (args.scan_b,),
            )
        )
        return 0
    if cmd == "sources":
        if args.sources_command == "list":
            print(
                d.fetch_all(
                    "SELECT id,display_name,last_mount_path,last_seen_at FROM source_roots ORDER BY id"
                )
            )
        elif args.sources_command == "show":
            print(d.fetch_one("SELECT * FROM source_roots WHERE id=?", (args.source_id,)))
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
                    "resume scheduled; rerun the corresponding analyzer command to continue its idempotent content work"
                )
            return 0
        print(
            d.fetch_all(
                "SELECT * FROM jobs WHERE id=?"
                if args.jobs_command == "show"
                else "SELECT id,job_type,status,processed_count,total_estimate,updated_at FROM jobs ORDER BY id DESC",
                (args.job_id,) if args.jobs_command == "show" else (),
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
            print(
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
            print(
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
            print(d.fetch_one("SELECT * FROM review_sessions WHERE id=?", (args.session_id,)))
        return 0
    if cmd == "benchmark":
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
        print(d.fetch_one("SELECT * FROM jobs WHERE id=?", (args.job_id,)))
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
        from .graph.builder import build_projection
        import json

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
                    d, args.projection, max_nodes=args.max_nodes, max_edges=args.max_edges
                ),
            )
            if args.graph_command == "export":
                _run_job(
                    d,
                    c,
                    "GRAPH_EXPORT",
                    {"projection": args.projection, "output": args.output},
                    lambda _job: Path(args.output).write_text(
                        json.dumps(payload, indent=2), encoding="utf-8"
                    ),
                )
                print(args.output)
            else:
                print(json.dumps(payload, indent=2))
        return 0
    if cmd == "dashboard":
        import uvicorn
        from .dashboard.app import create_app

        host = args.host or c.data["dashboard"]["host"]
        if host not in {"127.0.0.1", "localhost", "::1"} and not c.data["dashboard"].get(
            "allow_non_loopback", False
        ):
            raise SystemExit(
                "refusing non-loopback dashboard binding; set dashboard.allow_non_loopback=true explicitly"
            )
        uvicorn.run(
            create_app(d, args.read_only),
            host=host,
            port=args.port or c.data["dashboard"]["port"],
            log_level="info",
        )
        return 0
    return 0
