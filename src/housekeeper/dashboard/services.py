"""Read-only dashboard queries, intentionally separate from HTTP rendering."""

from __future__ import annotations

import json
import time

from .filters import ReviewFilter
from .view_models import Chart, Metric, OverviewViewModel, ReviewRow

# In-process safety net on top of the materialized summaries: even a burst of concurrent loads
# recomputes the view model at most once every TTL. The summaries themselves only change on a
# scan/analyze or an explicit "Refresh now", so this is generous.
OVERVIEW_TTL_SECONDS = 45.0

# Materialized chart key -> the title the overview template keys its bar rendering off of.
_CHART_TITLES = {
    "file_types": "File types",
    "classification_bytes": "Classification bytes",
    "top_level": "Top-level directories",
    "scan_history": "Scan history",
    "analyzer_completion": "Analyzer completion",
}


class DashboardService:
    def __init__(self, database):
        self.database = database
        self._overview_cache: tuple[float, OverviewViewModel] | None = None

    def invalidate_overview(self) -> None:
        """Drop the cached overview so the next load reflects a fresh refresh immediately."""
        self._overview_cache = None

    def overview(self) -> OverviewViewModel:
        cached = self._overview_cache
        if cached is not None and time.monotonic() - cached[0] < OVERVIEW_TTL_SECONDS:
            return cached[1]
        model = self._build_overview()
        self._overview_cache = (time.monotonic(), model)
        return model

    def _summary(self, key: str) -> tuple[dict, str | None]:
        """A materialized summary's parsed value and its refreshed_at, or ({}, None) if absent."""
        row = self.database.fetch_one(
            "SELECT value_json,refreshed_at FROM materialized_summaries WHERE summary_key=?", (key,)
        )
        if not row:
            return {}, None
        return json.loads(row["value_json"]), row["refreshed_at"]

    def _build_overview(self) -> OverviewViewModel:
        # Served entirely from materialized_summaries + one tiny live jobs count — no full-table
        # scan runs on a normal page load regardless of inventory size. The summaries are refreshed
        # at the end of every scan/analyze and on demand via "Refresh now".
        overview, refreshed_at = self._summary("overview")
        classifications, _ = self._summary("classifications")
        charts_data, _ = self._summary("charts")
        # Active-job count stays live: it is volatile and cheap (indexed, tiny jobs table), and a
        # materialized value would lie about a job started seconds ago.
        active_jobs = int(
            self.database.fetch_one(
                "SELECT COUNT(*) n FROM jobs WHERE status IN ('PENDING','RUNNING','PAUSED','PAUSING','CANCELLING')"
            )["n"]
        )
        logical_bytes = int(overview.get("logical_bytes", 0))
        unique_bytes = int(overview.get("unique_content_bytes", 0))
        metrics = (
            Metric("Sources", int(overview.get("sources", 0))),
            Metric("Entries", int(overview.get("entries", 0)), href="/review"),
            Metric("Content objects", int(overview.get("content_objects", 0)), href="/graph"),
            Metric("Artifacts", int(overview.get("analysis_artifacts", 0)), href="/jobs"),
            Metric(
                "Duplicate groups",
                int(overview.get("duplicate_groups", 0)),
                href="/duplicates",
            ),
            Metric("Logical bytes", logical_bytes, kind="bytes", href="/review"),
            Metric("Unique bytes", unique_bytes, kind="bytes", href="/graph"),
            Metric(
                "Protected",
                int(classifications.get("PROTECTED", 0)),
                href="/review?protected=true",
            ),
            Metric("Active jobs", active_jobs, href="/jobs"),
        )
        charts = tuple(
            Chart(
                _CHART_TITLES[key],
                tuple(charts_data[key]["columns"]),
                tuple(tuple(row) for row in charts_data[key]["rows"]),
            )
            for key in _CHART_TITLES
            if key in charts_data
        )
        integrity = "not checked" if refreshed_at else "not yet computed"
        return OverviewViewModel(
            integrity, max(0, logical_bytes - unique_bytes), metrics, charts, refreshed_at
        )

    def review_rows(self, filters: ReviewFilter, limit: int, after_id: int) -> list[ReviewRow]:
        where, params = filters.where_clause()
        rows = self.database.fetch_all(
            f"""SELECT e.id,e.name,e.relative_path,e.source_root,e.size_bytes,e.modified_at,c.classification,c.confidence,
                COALESCE(d.decision,'') decision,COALESCE(c.reason_codes_json,'[]') reason_codes,COALESCE(d.user_note,'') notes,COALESCE(d.stale,0) stale,
                EXISTS(SELECT 1 FROM exact_duplicate_members dm WHERE dm.entry_id=e.id) duplicate_member,
                EXISTS(SELECT 1 FROM projects p WHERE p.root_entry_id=e.id) project_member,
                (SELECT rg.id FROM exact_duplicate_members edm
                 JOIN entry_content_links ecl ON ecl.entry_id=edm.entry_id
                 JOIN relationship_group_members rgm ON rgm.content_object_id=ecl.content_object_id
                 JOIN relationship_groups rg ON rg.id=rgm.group_id AND rg.group_type='IMAGE_SIMILARITY'
                 WHERE edm.entry_id=e.id ORDER BY rg.id LIMIT 1) image_group_id
                FROM filesystem_entries e LEFT JOIN classifications c ON c.entry_id=e.id
                LEFT JOIN review_decisions d ON d.target_type='ENTRY' AND d.target_id=e.id AND d.current=1
                WHERE {where} AND e.id>? ORDER BY e.id LIMIT ?""",
            (*params, after_id, limit),
        )
        return [
            ReviewRow(
                entry_id=int(row["id"]),
                name=str(row["name"]),
                relative_path=str(row["relative_path"]),
                source_root=str(row["source_root"]),
                top_level_directory=str(row["relative_path"]).split("/", 1)[0],
                size_bytes=int(row["size_bytes"] or 0),
                modified_at=row["modified_at"],
                classification=row["classification"],
                confidence=row["confidence"],
                decision=str(row["decision"]) or None,
                reason_codes=str(row["reason_codes"]),
                notes=str(row["notes"]),
                stale=bool(row["stale"]),
                duplicate_member=bool(row["duplicate_member"]),
                project_member=bool(row["project_member"]),
                image_group_id=(int(row["image_group_id"]) if row["image_group_id"] else None),
            )
            for row in rows
        ]
