"""Typed data passed from dashboard services into Jinja templates."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Metric:
    label: str
    value: int
    kind: str = "count"
    href: str | None = None
    description: str = ""


@dataclass(frozen=True)
class Chart:
    title: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]


@dataclass(frozen=True)
class OverviewViewModel:
    integrity: str
    reclaimable_bytes: int
    metrics: tuple[Metric, ...]
    charts: tuple[Chart, ...]
    refreshed_at: str | None = None


@dataclass(frozen=True)
class ReviewRow:
    entry_id: int
    name: str
    relative_path: str
    source_root: str
    top_level_directory: str
    size_bytes: int
    modified_at: float | None
    classification: str | None
    confidence: float | None
    decision: str | None
    reason_codes: str
    notes: str
    stale: bool
    duplicate_member: bool
    project_member: bool
    image_group_id: int | None
