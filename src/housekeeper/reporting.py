"""Backward-compatible entry point; real implementation lives in ``housekeeper.reports``."""

from .reports.exports import export_csv, export_jsonl
from .reports.generator import generate_all_reports, generate_report, render_template

__all__ = [
    "export_csv",
    "export_jsonl",
    "generate_all_reports",
    "generate_report",
    "render_template",
]
