"""Render report contexts through Jinja2 templates and write static HTML + exports."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

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


def render_template(template_name: str, context: dict, output_path: Path) -> Path:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    html = _environment().get_template(template_name).render(**context)
    output_path.write_text(html, encoding="utf-8")
    return output_path


def _reports_dir(config) -> Path:
    return config.workspace / config.data["workspace"]["reports_dir"]


def generate_report(report_type: str, database, config) -> Path:
    if report_type not in CONTEXT_BUILDERS:
        raise ValueError(f"unknown report type: {report_type}")
    context = CONTEXT_BUILDERS[report_type](database, config)
    context.setdefault("report_type", report_type)
    return render_template(f"{report_type}.html.j2", context, _reports_dir(config) / f"{report_type}.html")


def generate_all_reports(database, config) -> list[Path]:
    out = _reports_dir(config)
    paths = [generate_report(name, database, config) for name in CONTEXT_BUILDERS]
    paths.append(export_csv(database, out / "recommendations.csv"))
    paths.append(export_jsonl(database, out / "recommendations.jsonl"))
    return paths
