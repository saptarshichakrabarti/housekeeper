import argparse
import sys
from pathlib import Path

from .analyzers.exact_duplicates import run_exact_duplicate_analysis
from .config import load_config
from .database import Database
from .logging_utils import configure_logging
from .manifests import (export_review_manifest, load_manifest,
                        validate_manifest_against_database,
                        validate_manifest_schema)
from .policies import classify_all_entries
from .reporting import generate_all_reports, generate_report
from .restore import restore_transaction
from .review_mover import move_approved_entries
from .scanner import DriveScanner


def _ctx(args):
    c = load_config(
        Path(args.config) if args.config else None,
        Path(args.workspace) if args.workspace else None,
    )
    c.workspace.mkdir(parents=True, exist_ok=True)
    configure_logging(c.workspace / c.data["workspace"]["logs_dir"])
    d = Database(c.database_path)
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
    sub.add_parser("scan-status")
    sub.add_parser("stats")
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
            "all",
        ],
    )
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
    m = sub.add_parser("move-to-review")
    m.add_argument("manifest")
    m.add_argument("review_root")
    m.add_argument("--dry-run", action="store_true")
    m.add_argument("--yes", action="store_true")
    rr = sub.add_parser("restore")
    rr.add_argument("transaction_manifest")
    rr.add_argument("--dry-run", action="store_true")
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    c, d = _ctx(args)
    cmd = args.command
    if cmd == "init-workspace":
        print(f"Workspace ready: {c.workspace}")
        return 0
    if cmd == "scan":
        print(DriveScanner(d, c).scan(Path(args.source_root), not args.no_resume))
        return 0
    if cmd == "analyze":
        if args.kind in {"exact-duplicates", "all"}:
            run_exact_duplicate_analysis(d, c)
        return 0
    if cmd == "classify":
        classify_all_entries(d, c)
        return 0
    if cmd == "report":
        print(
            [
                p.as_posix()
                for p in (
                    generate_all_reports(d, c)
                    if args.kind == "all"
                    else [generate_report(args.kind, d, c)]
                )
            ]
        )
        return 0
    if cmd == "export-review":
        export_review_manifest(
            d, Path(args.output), set(args.classifications.split(","))
        )
        return 0
    if cmd == "validate-manifest":
        es = load_manifest(Path(args.manifest))
        errors = validate_manifest_schema(es) + validate_manifest_against_database(
            es, d
        )
        print("valid" if not errors else "\n".join(errors))
        return 0 if not errors else 2
    if cmd == "move-to-review":
        tx = move_approved_entries(
            load_manifest(Path(args.manifest)),
            Path(args.review_root),
            d,
            args.dry_run,
            args.yes,
        )
        print(tx)
        return 0
    if cmd == "restore":
        print(restore_transaction(Path(args.transaction_manifest), args.dry_run))
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
    return 0
