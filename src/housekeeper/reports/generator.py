"""Render report contexts through Jinja2 templates and write static HTML + exports."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from ..jobs import checkpoint, update_job
from .contexts import CONTEXT_BUILDERS
from .exports import export_csv, export_jsonl
from .formatting import human_size, percent

_TEMPLATE_DIR = Path(__file__).parent / "templates"


@lru_cache(maxsize=1)
def _environment():
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    env = Environment(
        loader=FileSystemLoader(str(_TEMPLATE_DIR)),
        autoescape=select_autoescape(["html", "j2", "html.j2"]),
    )
    env.filters["human_size"] = human_size
    env.filters["percent"] = percent
    return env


def render_template(template_name: str, context: dict, output_path: Path, marker: str = "") -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    html = _environment().get_template(template_name).render(**context)
    # The marker goes last: a comment *before* the doctype puts browsers into quirks mode.
    output_path.write_text(html + (f"\n{marker}\n" if marker else ""), encoding="utf-8")
    return output_path


def _reports_dir(config) -> Path:
    return config.workspace / config.data["workspace"]["reports_dir"]


def _input_marker(report_type: str, database, config) -> str:
    """The fingerprint of everything this report is derived from, as an HTML comment.

    Stored in the report itself rather than in a table: a deleted or hand-edited report then
    regenerates on its own, with no bookkeeping that could fall out of step with the filesystem.

    A report is derived from the snapshot *and* from the analysis over it, so both are in the
    digest — a standalone ``analyse`` run with no rescan still refreshes the reports.
    """
    from ..config import config_fingerprint
    from ..reuse import derived_state_token, input_fingerprint, inventory_token

    fingerprint = input_fingerprint(
        report_type,
        f"{inventory_token(database)}|{derived_state_token(database)}",
        config_fingerprint(config),
    )
    return f"<!-- housekeeper-input {fingerprint} -->"


def _up_to_date(output_path: Path, marker: str) -> bool:
    try:
        with output_path.open("rb") as fh:
            fh.seek(max(0, output_path.stat().st_size - 256))
            return marker.encode() in fh.read()
    except OSError:
        return False


def generate_report(report_type: str, database, config, reuse: bool = True) -> Path:
    """Write one report. Unchanged inputs and an intact output file mean it is left alone."""
    # The CLI spells its kinds with hyphens (`report exact-duplicates`) and the builders with
    # underscores; accept either rather than failing on the documented spelling.
    report_type = report_type.replace("-", "_")
    if report_type not in CONTEXT_BUILDERS:
        raise ValueError(f"unknown report type: {report_type}")
    output_path = _reports_dir(config) / f"{report_type}.html"
    marker = _input_marker(report_type, database, config)
    if reuse and _up_to_date(output_path, marker):
        return output_path
    context = CONTEXT_BUILDERS[report_type](database, config)
    context.setdefault("report_type", report_type)
    return render_template(f"{report_type}.html.j2", context, output_path, marker)


def generate_all_reports(
    database, config, job_id: int | None = None, reuse: bool = True
) -> list[Path]:
    total = len(CONTEXT_BUILDERS) + 2  # every report type, plus the CSV and JSONL exports
    if job_id:
        update_job(database, job_id, total_estimate=total)
    out = _reports_dir(config)
    paths = []
    for name in CONTEXT_BUILDERS:
        paths.append(generate_report(name, database, config, reuse=reuse))
        checkpoint(database, job_id, processed_count=len(paths))
    for name, exporter in (
        ("recommendations.csv", export_csv),
        ("recommendations.jsonl", export_jsonl),
    ):
        paths.append(_export(out / name, _input_marker(name, database, config), exporter, database, config, reuse))
        checkpoint(database, job_id, processed_count=len(paths))
    return paths


def _export(path: Path, marker: str, exporter, database, config, reuse: bool) -> Path:
    """Write an export unless it is already current, tracked by a sidecar fingerprint file.

    CSV has no comment syntax and a JSONL stream should stay parseable, so the marker cannot live in
    the file the way a report's does. A sidecar next to it keeps ``report all`` a genuine no-op on an
    untouched workspace, and is self-correcting: delete either file and the export runs again.
    """
    sidecar = path.with_suffix(path.suffix + ".fingerprint")
    if reuse and path.exists() and _read_text(sidecar) == marker:
        return path
    written = exporter(database, path, config)
    sidecar.write_text(marker, encoding="utf-8")
    return written


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""
