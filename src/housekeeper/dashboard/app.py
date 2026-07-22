"""Local, bounded dashboard.  It can record review decisions but never moves data."""

from html import escape
import hashlib
import json
from pathlib import Path
import secrets
from typing import Annotated
from urllib.parse import urlencode

from ..config import AppConfig
from ..core.progress import eta_seconds, format_duration, seconds_since, throughput


def create_app(
    database,
    read_only: bool = False,
    contact_sheet_dir: Path | None = None,
    config: AppConfig | None = None,
):
    try:
        from fastapi import FastAPI, Form, Header, HTTPException, Path as ApiPath, Query
        from fastapi.responses import FileResponse, HTMLResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:
        raise RuntimeError(
            "Install the dashboard extra: pip install 'drive-housekeeper[dashboard]'"
        ) from exc
    from ..graph.builder import build_projection
    from ..review.decisions import record_decision
    from .filters import ReviewFilter, filesizeformat, relativetime, thousands
    from .services import DashboardService
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    from markupsafe import Markup

    csrf_token = secrets.token_urlsafe(24)
    static_dir = Path(__file__).with_name("static")
    templates = Environment(
        loader=FileSystemLoader(Path(__file__).with_name("templates")),
        autoescape=select_autoescape(["html", "xml"]),
    )
    templates.filters.update(
        filesizeformat=filesizeformat,
        thousands=thousands,
        relativetime=relativetime,
    )
    app = FastAPI(title="drive_housekeeper", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    # Every read-only surface (the service and all GET endpoints) goes through the per-thread
    # read-only connection pool; only the writer connection (`database`) records decisions, runs
    # control operations, and reconciles jobs. WAL lets the many readers run without serializing
    # against each other or the background runner's writes.
    reader = database.reader()
    service = DashboardService(reader)
    runner = None
    if config is not None and not read_only:
        from .runner import OperationRunner

        runner = OperationRunner(config)

    def maybe_reconcile() -> None:
        """Settle orphaned jobs so the UI reflects reality, never a phantom running operation.

        Read-only dashboards observe without mutating, so they skip it. Everywhere else this is
        cheap (it scans only the handful of non-terminal job rows) and must never surface an error
        into a page render, so any failure is swallowed — a poll tick is not the place to crash."""
        if read_only:
            return
        try:
            from ..jobs import reconcile_stale_jobs

            reconcile_stale_jobs(database)
        except Exception:  # noqa: BLE001 - reconciliation is best-effort housekeeping
            pass

    # Reap jobs stranded by a previous process (a killed CLI run, a restarted dashboard) up front,
    # so the very first page load already shows honest state instead of stale "running" rows.
    maybe_reconcile()
    navigation: list[dict[str, object]] = []
    action_items: list[tuple[str, str]] = []
    if runner is not None:
        action_items.append(("control", "Run"))
    if action_items:
        navigation.append({"label": "Actions", "items": action_items})
    navigation.extend(
        [
            {
                "label": "Review",
                "items": [
                    ("review", "Review"),
                    ("duplicates", "Duplicates"),
                    ("advanced-duplicates", "Advanced dupes"),
                    ("chunk-overlap", "Chunk overlap"),
                ],
            },
            {
                "label": "Insights",
                "items": [
                    ("documents", "Documents"),
                    ("images", "Images"),
                    ("projects", "Projects"),
                    ("events", "Events"),
                    ("derivations", "Derivations"),
                    ("backups", "Backups"),
                    ("record-series", "Record series"),
                    ("preservation", "Preservation"),
                ],
            },
            {
                "label": "System",
                "items": [
                    ("jobs", "Jobs"),
                    ("graph", "Graph"),
                    ("learning", "Learning"),
                    ("manifests", "Manifests"),
                ],
            },
        ]
    )

    def guard(token: str | None) -> None:
        if read_only:
            raise HTTPException(403, "dashboard is read-only")
        if token != csrf_token:
            raise HTTPException(403, "CSRF validation failed")

    def page(
        title: str, body: str, *, scripts: str = "", active_path: str = ""
    ) -> HTMLResponse:
        return HTMLResponse(
            templates.get_template("base.html").render(
                title=title,
                body=Markup(body),
                scripts=Markup(scripts),
                app_css_version=(static_dir / "app.css").stat().st_mtime_ns,
                theme_switch_version=(static_dir / "theme-switch.js").stat().st_mtime_ns,
                csrf_token=csrf_token,
                navigation=navigation,
                active_path=active_path,
                dashboard_js_version=(static_dir / "dashboard.js").stat().st_mtime_ns,
            )
        )

    def template_page(
        title: str,
        template_name: str,
        *,
        scripts: str = "",
        active_path: str = "",
        **context,
    ) -> HTMLResponse:
        body = templates.get_template(template_name).render(**context)
        return page(title, body, scripts=scripts, active_path=active_path)

    BYTE_COLUMNS = {
        "bytes",
        "size_bytes",
        "shared_chunk_bytes",
        "source_size_bytes",
        "generated_size_bytes",
        "environment_size_bytes",
        "recursive_size_bytes",
        "reclaimable_bytes",
        "bytes_seen",
    }
    TIME_COLUMNS = {"updated_at", "completed_at", "created_at", "modified_at", "started_at"}
    COUNT_COLUMNS = {
        "id",
        "files",
        "count",
        "member_count",
        "assigned",
        "training_count",
        "files_seen",
        "success_count",
        "skip_count",
        "error_count",
        "processed_count",
        "total_estimate",
    }

    def display_cell(heading: str, value: object) -> str:
        if value is None or value == "":
            return ""
        if heading in BYTE_COLUMNS or heading.endswith("_bytes"):
            return str(filesizeformat(value))
        if heading in TIME_COLUMNS or heading.endswith("_at"):
            return str(relativetime(value))
        if heading in COUNT_COLUMNS or heading.endswith("_count"):
            return escape(thousands(value))
        return escape(str(value))

    def rows_table(
        rows,
        headings: list[str],
        empty_message: str | None = None,
    ) -> str:
        header = "".join(f"<th>{escape(h)}</th>" for h in headings)
        body = "".join(
            "<tr>"
            + "".join(
                f"<td>{display_cell(h, row[h] if h in row.keys() else None)}</td>"
                for h in headings
            )
            + "</tr>"
            for row in rows
        )
        if empty_message is None:
            empty_html = (
                'No results yet — run the relevant analysis from the <a href="/control">Run page</a>.'
                if runner is not None
                else "No results yet — analysis has not produced matching records."
            )
        else:
            empty_html = escape(empty_message)
        empty = f'<tr><td class="empty-state" colspan="99">{empty_html}</td></tr>'
        return f'<div class="table-scroll"><table><thead><tr>{header}</tr></thead><tbody>{body or empty}</tbody></table></div>'

    # Human labels for jobs that are not actively being worked. A stopped job must read as stopped.
    STOPPED_LABELS = {
        "PENDING": "queued",
        "PAUSED": "paused",
        "CANCELLED": "cancelled",
        "INTERRUPTED": "interrupted",
        "FAILED": "failed",
        "COMPLETED": "completed",
        "COMPLETED_WITH_ERRORS": "completed with errors",
    }
    DANGER_STATES = {"FAILED", "CANCELLED", "INTERRUPTED"}

    def progress_cell(row) -> str:
        """Render a job row's progress honestly for its status.

        Only a job with a live worker (RUNNING/PAUSING/CANCELLING) shows a moving rate, an ETA, or
        an animated indeterminate bar. Anything stopped — completed, cancelled, interrupted, failed,
        paused, or still queued — renders as static text (and a static, valued bar when a total is
        known), so a job the worker abandoned can never masquerade as still churning. The previous
        version emitted a live ``<progress>`` and a ``started_at``-derived rate for every row, which
        is why long-cancelled jobs kept animating with a decaying throughput in the UI.
        """
        status = row["status"]
        processed = row["processed_count"] or 0
        total = row["total_estimate"]
        danger = " class='hk-progress--danger'" if status in DANGER_STATES else ""
        if status in {"RUNNING", "PAUSING", "CANCELLING"}:
            rate = throughput(processed, seconds_since(row["started_at"]))
            stopping = " · stopping…" if status in {"PAUSING", "CANCELLING"} else ""
            if total:
                pct = min(100, int(processed * 100 / total))
                eta = eta_seconds(processed, total, rate)
                eta_html = f" · ETA {format_duration(eta)}" if eta is not None else ""
                return (
                    f"<progress value='{processed}' max='{total}'></progress> "
                    f"{pct}% {processed:,}/{total:,} · {rate:,.1f}/s{eta_html}{stopping}"
                )
            current = f" · {escape(str(row['current_item']))}" if row["current_item"] else ""
            return f"<progress></progress> {processed:,} processed · {rate:,.1f}/s{current}{stopping}"
        # Stopped: no animation, no fabricated rate — a frozen, factual snapshot.
        label = STOPPED_LABELS.get(status, status.lower())
        if total:
            pct = min(100, int(processed * 100 / total))
            return (
                f"<progress{danger} value='{processed}' max='{total}'></progress> "
                f"{label} · {pct}% {processed:,}/{total:,}"
            )
        return f"{label} · {processed:,} processed"

    def job_controls(job_id: int, status: str) -> str:
        """The control buttons a job's *current* status actually supports.

        Pause is only meaningful while a worker is running; Cancel stays available right through the
        stopping states (PAUSING/PAUSED) so a user is never left with a job they cannot stop.
        Transitional CANCELLING and terminal states offer nothing — a live worker settles the first
        and the reaper settles an orphan, so an extra button would be a lie about what the click
        does."""
        pause = (
            f"<button hx-post='/fragments/jobs/{job_id}/control?action=pause' "
            f"hx-target='closest tr'>Pause</button> "
        )
        cancel = (
            f"<button hx-post='/fragments/jobs/{job_id}/control?action=cancel' "
            f"hx-target='closest tr'>Cancel</button>"
        )
        if status in {"PENDING", "RUNNING"}:
            return pause + cancel
        if status in {"PAUSING", "PAUSED"}:
            return cancel
        return ""

    def jobs_table(rows) -> str:
        headings = [
            "id",
            "job_type",
            "status",
            "progress",
            "success_count",
            "skip_count",
            "error_count",
            "updated_at",
        ]
        header = "".join(f"<th>{escape(heading)}</th>" for heading in [*headings, "controls"])
        body = ""
        for row in rows:
            cells = "".join(
                f"<td>{progress_cell(row)}</td>"
                if heading == "progress"
                else f"<td>{display_cell(heading, row[heading])}</td>"
                for heading in headings
            )
            controls = job_controls(row["id"], row["status"])
            body += f"<tr>{cells}<td>{controls}</td></tr>"
        running = any(row["status"] in {"PENDING", "RUNNING", "PAUSING", "CANCELLING"} for row in rows)
        completed_count = next(
            (
                row["success_count"] or row["processed_count"] or 0
                for row in rows
                if row["status"] in {"COMPLETED", "COMPLETED_WITH_ERRORS"}
            ),
            0,
        )
        empty_message = (
            'No jobs yet — start a scan or analysis from the <a href="/control">Run page</a>.'
            if runner is not None
            else "No jobs have been recorded yet."
        )
        empty = f'<tr><td class="empty-state" colspan="99">{empty_message}</td></tr>'
        return (
            f'<div class="jobs-status" data-running="{str(running).lower()}" '
            f'data-completed-count="{int(completed_count)}">'
            f'<div class="table-scroll"><table><thead><tr>{header}</tr></thead>'
            f"<tbody>{body or empty}</tbody></table></div></div>"
        )

    def decision_manifest_records(session_id: int) -> list[dict[str, object]]:
        rows = reader.fetch_all(
            """SELECT e.id,e.absolute_path,e.relative_path,e.size_bytes,c.classification,c.confidence,c.reason_codes_json,c.explanation,s.full_hash,d.decision,d.stale
               FROM review_decisions d JOIN filesystem_entries e ON d.target_type='ENTRY' AND d.target_id=e.id
               LEFT JOIN classifications c ON c.entry_id=e.id LEFT JOIN file_signatures s ON s.entry_id=e.id
               WHERE d.review_session_id=? AND d.current=1 ORDER BY e.relative_path""",
            (session_id,),
        )
        return [
            {
                "approved": row["decision"] == "APPROVE_FOR_REVIEW" and not row["stale"],
                "entry_id": row["id"],
                "source_path": row["absolute_path"],
                "relative_path": row["relative_path"],
                "size_bytes": row["size_bytes"],
                "expected_sha256": row["full_hash"] or "",
                "classification": row["classification"] or "UNKNOWN",
                "confidence": row["confidence"] or 0,
                "reason_codes": json.loads(row["reason_codes_json"] or "[]"),
                "explanation": row["explanation"] or "",
                "canonical_surviving_path": "",
                "reviewer_notes": "",
                "decision": row["decision"],
                "stale": bool(row["stale"]),
                "review_session_id": session_id,
            }
            for row in rows
        ]

    def review_rows(
        limit: int,
        after_id: int,
        classification: str | None,
        extension: str | None = None,
        minimum_size: int | None = None,
        maximum_size: int | None = None,
        stale: bool | None = None,
    ):
        params: list[object] = [after_id]
        where = "e.id>?"
        if classification:
            where += " AND c.classification=?"
            params.append(classification)
        if extension:
            where += " AND e.suffix=?"
            params.append(extension.lower())
        if minimum_size is not None:
            where += " AND e.size_bytes>=?"
            params.append(minimum_size)
        if maximum_size is not None:
            where += " AND e.size_bytes<=?"
            params.append(maximum_size)
        if stale is not None:
            where += " AND EXISTS(SELECT 1 FROM review_decisions d WHERE d.target_type='ENTRY' AND d.target_id=e.id AND d.current=1 AND d.stale=?)"
            params.append(int(stale))
        params.append(limit)
        return reader.fetch_all(
            f"SELECT e.id,e.name,e.relative_path,e.size_bytes,e.modified_at,c.classification,c.confidence FROM filesystem_entries e LEFT JOIN classifications c ON c.entry_id=e.id WHERE {where} ORDER BY e.id LIMIT ?",
            tuple(params),
        )

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        if request.url.path.startswith("/static/"):
            # StaticFiles supplies ETags; require browsers to revalidate them so
            # dashboard CSS and JavaScript cannot get out of sync after an update.
            response.headers["Cache-Control"] = "no-cache"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/", response_class=HTMLResponse)
    def overview():
        return template_page(
            "Housekeeper overview",
            "overview.html",
            model=service.overview(),
            can_refresh=not read_only,
            has_run_page=runner is not None,
            active_path="",
        )

    @app.post("/refresh", response_class=HTMLResponse)
    def refresh_overview(x_csrf_token: str | None = Header(default=None)):
        # Recompute the materialized summaries on demand (the only path that runs the full-table
        # aggregates during a session). Non-read-only and CSRF-guarded via `guard`.
        guard(x_csrf_token)
        database.refresh_materialized_summaries()
        service.invalidate_overview()
        body = templates.get_template("overview.html").render(
            model=service.overview(), can_refresh=True, has_run_page=runner is not None
        )
        return HTMLResponse(body)

    @app.get("/review", response_class=HTMLResponse)
    def review(
        limit: int = Query(100, ge=1, le=500),
        after_id: int = Query(0, ge=0),
        classification: str | None = None,
        extension: str | None = None,
        minimum_size: int | None = Query(None, ge=0),
        maximum_size: int | None = Query(None, ge=0),
        stale: bool | None = None,
        source_root_id: int | None = Query(None, ge=1),
        decision: str | None = None,
        reason_code: str | None = None,
        duplicate_only: bool = False,
        project_only: bool = False,
        protected: bool | None = None,
        top_level_directory: str | None = None,
        modified_after: float | None = None,
        modified_before: float | None = None,
    ):
        filters = ReviewFilter(
            classification=classification,
            suffix=extension,
            minimum_size=minimum_size,
            maximum_size=maximum_size,
            stale=stale,
            source_root_id=source_root_id,
            decision=decision,
            reason_code=reason_code,
            duplicate_only=duplicate_only,
            project_only=project_only,
            protected=protected,
            top_level_directory=top_level_directory,
            minimum_age_timestamp=modified_after,
            maximum_age_timestamp=modified_before,
        )
        rows = service.review_rows(filters, limit, after_id)
        filter_values: dict[str, object] = {
            "classification": classification,
            "extension": extension,
            "minimum_size": minimum_size,
            "maximum_size": maximum_size,
            "stale": stale,
            "source_root_id": source_root_id,
            "decision": decision,
            "reason_code": reason_code,
            "duplicate_only": duplicate_only or None,
            "project_only": project_only or None,
            "protected": protected,
            "top_level_directory": top_level_directory,
            "modified_after": modified_after,
            "modified_before": modified_before,
        }
        active_params = {
            key: value for key, value in filter_values.items() if value is not None and value != ""
        }
        chip_labels = {
            "classification": "Classification",
            "extension": "Extension",
            "minimum_size": "Minimum size",
            "maximum_size": "Maximum size",
            "stale": "Stale",
            "source_root_id": "Source",
            "decision": "Decision",
            "reason_code": "Reason",
            "duplicate_only": "Duplicates only",
            "project_only": "Projects only",
            "protected": "Protected",
            "top_level_directory": "Top-level directory",
            "modified_after": "Modified after",
            "modified_before": "Modified before",
        }
        chips = []
        for key, value in active_params.items():
            remaining = {name: item for name, item in active_params.items() if name != key}
            chips.append(
                {
                    "label": chip_labels[key],
                    "value": value,
                    "remove_url": "/review" + (f"?{urlencode(remaining)}" if remaining else ""),
                }
            )
        next_id = rows[-1].entry_id if rows else None
        next_params = {"limit": limit, **active_params, "after_id": next_id}
        classifications = [
            str(row["classification"])
            for row in reader.fetch_all(
                "SELECT DISTINCT classification FROM classifications WHERE classification IS NOT NULL ORDER BY classification"
            )
        ]
        top_level_directories = [
            str(row["top_level"])
            for row in reader.fetch_all(
                "SELECT DISTINCT CASE WHEN instr(relative_path,'/')=0 THEN relative_path ELSE substr(relative_path,1,instr(relative_path,'/')-1) END top_level FROM filesystem_entries WHERE entry_type='file' ORDER BY top_level LIMIT 500"
            )
        ]
        return template_page(
            "Review queue",
            "review.html",
            rows=rows,
            next_id=next_id,
            next_url=f"/review?{urlencode(next_params)}" if next_id else "",
            filters=filter_values,
            chips=chips,
            classifications=classifications,
            top_level_directories=top_level_directories,
            available_image_groups={
                row.image_group_id
                for row in rows
                if row.image_group_id is not None
                and _contact_sheet_file(row.image_group_id) is not None
            },
            has_run_page=runner is not None,
            read_only=read_only,
            active_path="review",
        )

    @app.get("/fragments/review", response_class=HTMLResponse)
    def review_fragment(
        limit: int = Query(100, ge=1, le=500),
        after_id: int = Query(0, ge=0),
        classification: str | None = None,
        extension: str | None = None,
        minimum_size: int | None = Query(None, ge=0),
        maximum_size: int | None = Query(None, ge=0),
        stale: bool | None = None,
    ):
        return HTMLResponse(
            rows_table(
                review_rows(
                    limit, after_id, classification, extension, minimum_size, maximum_size, stale
                ),
                ["id", "name", "relative_path", "classification", "confidence", "size_bytes"],
            )
        )

    @app.get("/fragments/entry/{entry_id}", response_class=HTMLResponse)
    def entry_detail_fragment(entry_id: Annotated[int, ApiPath(ge=1)]):
        entry = reader.fetch_one(
            """SELECT e.id,e.name,e.relative_path,e.size_bytes,s.full_hash,s.hash_status,c.classification
               FROM filesystem_entries e LEFT JOIN file_signatures s ON s.entry_id=e.id
               LEFT JOIN classifications c ON c.entry_id=e.id WHERE e.id=?""",
            (entry_id,),
        )
        if not entry:
            raise HTTPException(404, "entry not found")
        artifacts = reader.fetch_all(
            """SELECT a.analyzer_name,a.status,a.completed_at FROM entry_content_links l
               JOIN analysis_artifacts a ON a.content_object_id=l.content_object_id
               WHERE l.entry_id=? ORDER BY a.completed_at DESC""",
            (entry_id,),
        )
        return HTMLResponse(
            templates.get_template("fragments/entry_detail.html").render(
                entry=dict(entry),
                artifacts=[dict(artifact) for artifact in artifacts],
                has_run_page=runner is not None,
            )
        )

    @app.post("/fragments/review/decision", response_class=HTMLResponse)
    def entry_decision_fragment(
        entry_id: Annotated[int, Form(ge=1)],
        session_id: Annotated[int, Form(ge=1)],
        decision: Annotated[str, Form()],
        note: Annotated[str, Form()] = "",
        x_csrf_token: str | None = Header(default=None),
    ):
        guard(x_csrf_token)
        allowed = {
            "APPROVE_FOR_REVIEW",
            "REJECT_RECOMMENDATION",
            "DEFER",
            "MARK_KEEP",
            "MARK_PROTECTED",
            "NEEDS_MORE_ANALYSIS",
        }
        if decision not in allowed:
            raise HTTPException(422, "invalid decision")
        record_decision(
            database, session_id, "ENTRY", entry_id, decision, user_note=note, source="dashboard"
        )
        return HTMLResponse("<p>Decision recorded.</p>")

    def explorer(
        path: str, title: str, query: str, headings: list[str], limit: int
    ) -> HTMLResponse:
        rows = reader.fetch_all(query, (limit,))
        return page(
            title,
            f"<p>Bounded explorer; results are read-only.</p>{rows_table(rows, headings)}",
            active_path=path,
        )

    def linked_rows_table(rows, headings: list[str], id_key: str, href_prefix: str) -> str:
        """A rows_table with a trailing detail-link column.

        The href is built exclusively from ``int(row[id_key])`` so no row value can inject markup.
        """
        header = "".join(f"<th>{escape(h)}</th>" for h in [*headings, "detail"])
        body = ""
        for row in rows:
            cells = "".join(
                f"<td>{display_cell(h, row[h] if h in row.keys() else None)}</td>"
                for h in headings
            )
            body += f"<tr>{cells}<td><a href='{href_prefix}/{int(row[id_key])}'>open</a></td></tr>"
        empty_message = (
            'No results yet — run the relevant analysis from the <a href="/control">Run page</a>.'
            if runner is not None
            else "No results yet — analysis has not produced matching records."
        )
        empty = f'<tr><td class="empty-state" colspan="99">{empty_message}</td></tr>'
        return f'<div class="table-scroll"><table><thead><tr>{header}</tr></thead><tbody>{body or empty}</tbody></table></div>'

    def directory_card(entry_id: int) -> dict | None:
        entry = reader.fetch_one(
            "SELECT id,name,relative_path,source_root_id FROM filesystem_entries WHERE id=? AND entry_type='directory'",
            (entry_id,),
        )
        if not entry:
            return None
        summary = reader.fetch_one(
            "SELECT recursive_file_count,recursive_directory_count,recursive_size_bytes,"
            "unique_full_hash_count,duplicate_file_count,earliest_modified_at,latest_modified_at "
            "FROM directory_summaries WHERE entry_id=?",
            (entry_id,),
        )
        return {**dict(entry), **(dict(summary) if summary else {})}

    @app.get("/duplicates", response_class=HTMLResponse)
    def duplicates(
        limit: int = Query(100, ge=1, le=500), sort: str = Query("reclaimable")
    ):
        order_by = {
            "reclaimable": "reclaimable_bytes DESC,g.id",
            "members": "g.member_count DESC,g.id",
            "size": "g.size_bytes DESC,g.id",
        }
        if sort not in order_by:
            raise HTTPException(422, "invalid duplicate sort")
        rows = reader.fetch_all(
            f"""SELECT g.id,g.full_hash,g.member_count,g.size_bytes,
                       g.size_bytes*(g.member_count-1) reclaimable_bytes,g.canonical_entry_id,
                       (SELECT rg.id FROM exact_duplicate_members dm
                        JOIN entry_content_links ecl ON ecl.entry_id=dm.entry_id
                        JOIN relationship_group_members rgm ON rgm.content_object_id=ecl.content_object_id
                        JOIN relationship_groups rg ON rg.id=rgm.group_id AND rg.group_type='IMAGE_SIMILARITY'
                        WHERE dm.group_id=g.id ORDER BY rg.id LIMIT 1) image_group_id
                FROM exact_duplicate_groups g ORDER BY {order_by[sort]} LIMIT ?""",
            (limit,),
        )
        groups = []
        for row in rows:
            group = dict(row)
            image_group_id = group.get("image_group_id")
            group["has_contact_sheet"] = bool(
                image_group_id and _contact_sheet_file(int(image_group_id)) is not None
            )
            groups.append(group)
        return template_page(
            "Duplicate explorer",
            "duplicates.html",
            groups=groups,
            sort=sort,
            has_run_page=runner is not None,
            active_path="duplicates",
        )

    @app.get("/fragments/duplicates/{group_id}", response_class=HTMLResponse)
    def duplicate_detail_fragment(group_id: Annotated[int, ApiPath(ge=1)]):
        group = reader.fetch_one("SELECT * FROM exact_duplicate_groups WHERE id=?", (group_id,))
        if not group:
            raise HTTPException(404, "duplicate group not found")
        members = reader.fetch_all(
            """SELECT e.relative_path,e.modified_at,m.is_canonical,m.readable
               FROM exact_duplicate_members m JOIN filesystem_entries e ON e.id=m.entry_id
               WHERE m.group_id=? ORDER BY m.is_canonical DESC,e.relative_path""",
            (group_id,),
        )
        return HTMLResponse(
            templates.get_template("fragments/duplicate_detail.html").render(
                group=dict(group), members=[dict(member) for member in members]
            )
        )

    @app.get("/advanced-duplicates", response_class=HTMLResponse)
    def advanced_duplicates(limit: int = Query(100, ge=1, le=500)):
        return explorer(
            "advanced-duplicates",
            "Advanced duplicate explorer (tiered relationships)",
            "SELECT relationship_type,evidence_tier,source_id,target_id,round(confidence,3) confidence,explanation FROM content_relationships WHERE status='ACTIVE' ORDER BY evidence_tier,id LIMIT ?",
            ["relationship_type", "evidence_tier", "source_id", "target_id", "confidence", "explanation"],
            limit,
        )

    @app.get("/chunk-overlap", response_class=HTMLResponse)
    def chunk_overlap(limit: int = Query(100, ge=1, le=500)):
        return explorer(
            "chunk-overlap",
            "Partial-content overlap explorer",
            "SELECT content_object_a_id,content_object_b_id,shared_chunk_bytes,round(overlap_a_in_b,3) overlap_a_in_b,round(overlap_b_in_a,3) overlap_b_in_a,round(weighted_jaccard,3) weighted_jaccard FROM content_overlap_results ORDER BY shared_chunk_bytes DESC LIMIT ?",
            ["content_object_a_id", "content_object_b_id", "shared_chunk_bytes", "overlap_a_in_b", "overlap_b_in_a", "weighted_jaccard"],
            limit,
        )

    @app.get("/derivations", response_class=HTMLResponse)
    def derivations(limit: int = Query(100, ge=1, le=500)):
        rows = reader.fetch_all(
            "SELECT relationship_type,evidence_tier,source_id,target_id,round(confidence,3) confidence,explanation"
            " FROM content_relationships WHERE status='ACTIVE' AND relationship_type LIKE 'LIKELY_%' ORDER BY id LIMIT ?",
            (limit,),
        )
        table = linked_rows_table(
            rows,
            ["relationship_type", "evidence_tier", "source_id", "target_id", "confidence", "explanation"],
            "source_id",
            "/derivations",
        )
        return page(
            "Derivation-family explorer",
            "<p>Bounded explorer; results are read-only. "
            "Open a source object for its derivation timeline.</p>" + table,
            active_path="derivations",
        )

    def _representative_entry(content_object_id: int) -> dict | None:
        row = reader.fetch_one(
            "SELECT e.name,e.relative_path,e.modified_at FROM entry_content_links l "
            "JOIN filesystem_entries e ON e.id=l.entry_id WHERE l.content_object_id=? ORDER BY e.id LIMIT 1",
            (content_object_id,),
        )
        return dict(row) if row else None

    @app.get("/derivations/{content_object_id}", response_class=HTMLResponse)
    def derivation_timeline(content_object_id: Annotated[int, ApiPath(ge=1)]):
        relationships = reader.fetch_all(
            "SELECT * FROM content_relationships WHERE status='ACTIVE' AND relationship_type LIKE 'LIKELY_%'"
            " AND source_type='CONTENT_OBJECT' AND (source_id=? OR target_id=?) ORDER BY id LIMIT 200",
            (content_object_id, content_object_id),
        )
        if not relationships:
            raise HTTPException(404, "no derivation relationships for that content object")
        items = []
        for relationship in relationships:
            source = _representative_entry(int(relationship["source_id"]))
            target = _representative_entry(int(relationship["target_id"]))
            evidence = json.loads(relationship["evidence_json"] or "{}")
            items.append(
                {
                    "relationship_type": relationship["relationship_type"],
                    "evidence_tier": relationship["evidence_tier"],
                    "confidence": round(float(relationship["confidence"]), 3),
                    "source": source or {"name": f"content object {relationship['source_id']}"},
                    "target": target or {"name": f"content object {relationship['target_id']}"},
                    "gap_seconds": evidence.get("modified_gap_seconds"),
                    "explanation": relationship["explanation"],
                }
            )
        # Chronological by the derived side's modified time; unknown times sort last.
        items.sort(key=lambda item: item["target"].get("modified_at") or float("inf"))
        return template_page(
            "Derivation timeline",
            "derivation_timeline.html",
            content_object_id=content_object_id,
            items=items,
            active_path="derivations",
        )

    @app.get("/events", response_class=HTMLResponse)
    def events(limit: int = Query(100, ge=1, le=500)):
        return explorer(
            "events",
            "Event & collection explorer",
            "SELECT id,cluster_type,name,round(confidence,3) confidence,summary_json FROM collection_clusters ORDER BY id DESC LIMIT ?",
            ["id", "cluster_type", "name", "confidence", "summary_json"],
            limit,
        )

    @app.get("/record-series", response_class=HTMLResponse)
    def record_series(limit: int = Query(100, ge=1, le=500)):
        return explorer(
            "record-series",
            "Record-series explorer",
            "SELECT s.name,COUNT(a.id) assigned FROM record_series s LEFT JOIN record_series_assignments a ON a.series_id=s.id AND a.target_type='ENTRY' GROUP BY s.name ORDER BY assigned DESC LIMIT ?",
            ["name", "assigned"],
            limit,
        )

    @app.get("/preservation", response_class=HTMLResponse)
    def preservation(limit: int = Query(100, ge=1, le=500)):
        return explorer(
            "preservation",
            "Preservation queue (separate from clutter review)",
            "SELECT target_id,recommended_action,format_risk,encryption_risk,integrity_risk,accessibility_risk FROM preservation_assessments ORDER BY id LIMIT ?",
            ["target_id", "recommended_action", "format_risk", "encryption_risk", "integrity_risk", "accessibility_risk"],
            limit,
        )

    @app.get("/learning", response_class=HTMLResponse)
    def learning(limit: int = Query(100, ge=1, le=500)):
        return explorer(
            "learning",
            "Active-learning models (suggestions only; cannot approve movement)",
            "SELECT id,model_type,model_version,training_count,active,metrics_json FROM review_learning_models ORDER BY id DESC LIMIT ?",
            ["id", "model_type", "model_version", "training_count", "active", "metrics_json"],
            limit,
        )

    @app.get("/backups", response_class=HTMLResponse)
    def backups(limit: int = Query(100, ge=1, le=500)):
        rows = reader.fetch_all(
            "SELECT id,source_type,source_id,target_type,target_id,relationship_type,round(confidence,3) confidence"
            " FROM relationships WHERE relationship_type LIKE '%BACKUP%' OR relationship_type='MOSTLY_CONTAINED_IN'"
            " ORDER BY confidence DESC,id LIMIT ?",
            (limit,),
        )
        table = linked_rows_table(
            rows,
            ["id", "source_type", "source_id", "target_type", "target_id", "relationship_type", "confidence"],
            "id",
            "/backups",
        )
        return page(
            "Backup comparison",
            "<p>Bounded explorer; results are read-only. "
            "Open a relationship for a side-by-side directory comparison.</p>" + table,
            active_path="backups",
        )

    @app.get("/backups/{relationship_id}", response_class=HTMLResponse)
    def backup_compare(relationship_id: Annotated[int, ApiPath(ge=1)]):
        relationship = reader.fetch_one(
            "SELECT * FROM relationships WHERE id=? AND source_type='DIRECTORY' AND target_type='DIRECTORY'",
            (relationship_id,),
        )
        if not relationship:
            raise HTTPException(404, "directory relationship not found")
        left = directory_card(int(relationship["source_id"]))
        right = directory_card(int(relationship["target_id"]))
        if not left or not right:
            raise HTTPException(404, "directory entries not found")
        evidence = json.loads(relationship["evidence_json"] or "{}")
        return template_page(
            "Backup compare",
            "backup_compare.html",
            relationship=dict(relationship),
            left=left,
            right=right,
            evidence=evidence,
            active_path="backups",
        )

    @app.get("/documents", response_class=HTMLResponse)
    def documents(limit: int = Query(100, ge=1, le=500)):
        return explorer(
            "documents",
            "Document-version explorer",
            "SELECT content_object_id,analyzer_version,status,completed_at,error_code FROM analysis_artifacts WHERE analyzer_name='documents' ORDER BY completed_at DESC LIMIT ?",
            ["content_object_id", "analyzer_version", "status", "completed_at", "error_code"],
            limit,
        )

    @app.get("/images", response_class=HTMLResponse)
    def images(limit: int = Query(100, ge=1, le=500)):
        groups = reader.fetch_all(
            "SELECT g.id,g.group_key,COUNT(m.content_object_id) member_count FROM relationship_groups g"
            " JOIN relationship_group_members m ON m.group_id=g.id"
            " WHERE g.group_type='IMAGE_SIMILARITY' GROUP BY g.id ORDER BY member_count DESC,g.id LIMIT ?",
            (limit,),
        )
        artifacts = reader.fetch_all(
            "SELECT content_object_id,analyzer_version,status,completed_at,error_code FROM analysis_artifacts WHERE analyzer_name='images' ORDER BY completed_at DESC LIMIT ?",
            (limit,),
        )
        groups_table = linked_rows_table(
            groups, ["id", "group_key", "member_count"], "id", "/images"
        )
        artifacts_table = rows_table(
            artifacts,
            ["content_object_id", "analyzer_version", "status", "completed_at", "error_code"],
        )
        return page(
            "Image-similarity explorer",
            "<p>Bounded explorer; results are read-only. "
            "Open a group for its members and contact sheet.</p>"
            f"<h2>Similarity groups</h2>{groups_table}"
            f"<h2>Analysis artifacts</h2>{artifacts_table}",
            active_path="images",
        )

    def _contact_sheet_file(group_id: int) -> Path | None:
        if contact_sheet_dir is None:
            return None
        # The path is derived only from the validated integer id — never from request text.
        candidate = contact_sheet_dir / f"group_{group_id}.jpg"
        return candidate if candidate.is_file() else None

    @app.get("/images/{group_id}", response_class=HTMLResponse)
    def image_group_detail(group_id: Annotated[int, ApiPath(ge=1)]):
        group = reader.fetch_one(
            "SELECT id,group_key,evidence_json,created_at FROM relationship_groups"
            " WHERE id=? AND group_type='IMAGE_SIMILARITY'",
            (group_id,),
        )
        if not group:
            raise HTTPException(404, "image similarity group not found")
        members = reader.fetch_all(
            """SELECT m.content_object_id,
                      (SELECT e.relative_path FROM entry_content_links l JOIN filesystem_entries e ON e.id=l.entry_id
                       WHERE l.content_object_id=m.content_object_id ORDER BY e.id LIMIT 1) relative_path,
                      (SELECT a.artifact_json FROM analysis_artifacts a
                       WHERE a.analyzer_name='images' AND a.content_object_id=m.content_object_id
                       AND a.status='COMPLETED' LIMIT 1) artifact_json
               FROM relationship_group_members m WHERE m.group_id=? ORDER BY m.content_object_id LIMIT 200""",
            (group_id,),
        )
        detailed = []
        for member in members:
            artifact = json.loads(member["artifact_json"] or "{}")
            detailed.append(
                {
                    "content_object_id": member["content_object_id"],
                    "relative_path": member["relative_path"],
                    "format": artifact.get("format"),
                    "width": artifact.get("width"),
                    "height": artifact.get("height"),
                }
            )
        return template_page(
            "Image group",
            "image_group.html",
            group=dict(group),
            members=detailed,
            has_contact_sheet=_contact_sheet_file(group_id) is not None,
            active_path="images",
        )

    @app.get("/contact-sheets/{group_id}.jpg")
    def contact_sheet_image(group_id: Annotated[int, ApiPath(ge=1)]):
        sheet = _contact_sheet_file(group_id)
        if sheet is None:
            raise HTTPException(404, "contact sheet not rendered")
        return FileResponse(sheet, media_type="image/jpeg")

    @app.get("/projects", response_class=HTMLResponse)
    def projects(limit: int = Query(100, ge=1, le=500)):
        return explorer(
            "projects",
            "Project explorer",
            "SELECT id,name,kind,source_size_bytes,generated_size_bytes,environment_size_bytes,git_status FROM projects ORDER BY source_size_bytes DESC LIMIT ?",
            [
                "id",
                "name",
                "kind",
                "source_size_bytes",
                "generated_size_bytes",
                "environment_size_bytes",
                "git_status",
            ],
            limit,
        )

    @app.get("/jobs", response_class=HTMLResponse)
    def jobs(limit: int = Query(100, ge=1, le=500)):
        # Only a one-shot load: the fragment itself decides whether to keep polling (see below).
        return page(
            "Jobs",
            f"<section hx-get='/fragments/jobs?limit={limit}' hx-trigger='load'>Loading…</section>",
            active_path="jobs",
        )

    ACTIVE_JOB_STATES = {"PENDING", "RUNNING", "PAUSING", "CANCELLING"}

    @app.get("/fragments/jobs", response_class=HTMLResponse)
    def jobs_fragment(limit: int = Query(100, ge=1, le=500)):
        # The jobs fragment is polled by both the Jobs page and the overview, so it is the natural
        # heartbeat for reaping orphans: within one poll interval a dead worker's row turns honest.
        maybe_reconcile()
        rows = reader.fetch_all(
            "SELECT id,job_type,status,processed_count,total_estimate,success_count,skip_count,error_count,current_item,started_at,updated_at FROM jobs ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        # Self-suspending poll: keep the 3s cadence only while a job is actually active. When idle
        # the fragment re-arms on the `job-started` event alone (dispatched by the control endpoints
        # when an operation begins) so an idle dashboard issues no repeating job queries at all.
        active = any(row["status"] in ACTIVE_JOB_STATES for row in rows)
        trigger = "every 3s" if active else "job-started from:body"
        wrapper = (
            f"<div id='jobs-fragment' hx-get='/fragments/jobs?limit={limit}' "
            f"hx-trigger='{trigger}' hx-swap='outerHTML'>{jobs_table(rows)}</div>"
        )
        return HTMLResponse(wrapper)

    @app.post("/fragments/jobs/{job_id}/control", response_class=HTMLResponse)
    def job_control_fragment(
        job_id: Annotated[int, ApiPath(ge=1)],
        action: str = Query(...),
        x_csrf_token: str | None = Header(default=None),
    ):
        guard(x_csrf_token)
        if action not in {"pause", "cancel"}:
            raise HTTPException(422, "invalid job control")
        from ..jobs import request_cancel, request_pause

        try:
            if action == "pause":
                request_pause(database, job_id)
            else:
                request_cancel(database, job_id)
        except ValueError:
            # The job already moved past a state this action applies to (a double-click, or a
            # worker that finished first). Re-render the row's true state rather than erroring.
            pass
        # If the worker behind this job is already gone, finalize it now so the row the user gets
        # back reflects reality immediately instead of sitting in PAUSING/CANCELLING until a poll.
        maybe_reconcile()
        row = reader.fetch_one(
            "SELECT id,job_type,status,processed_count,total_estimate,success_count,skip_count,error_count,current_item,started_at,updated_at FROM jobs WHERE id=?",
            (job_id,),
        )
        if not row:
            raise HTTPException(404, "job not found")
        table = jobs_table([row])
        return HTMLResponse(table.split("<tbody>", 1)[1].split("</tbody>", 1)[0])

    if runner is not None:
        from .runner import ANALYZE_KINDS, REPORT_KINDS

        def control_fragment(job_started: bool = False) -> HTMLResponse:
            response = HTMLResponse(
                templates.get_template("fragments/control.html").render(
                    status=runner.status(),
                    read_only=read_only,
                    analyze_kinds=ANALYZE_KINDS,
                    report_kinds=REPORT_KINDS,
                )
            )
            if job_started:
                # Wake the (possibly suspended) jobs poll on any page that shows it.
                response.headers["HX-Trigger"] = "job-started"
            return response

        @app.get("/control", response_class=HTMLResponse)
        def control_page():
            folder_picker_version = (static_dir / "folder-picker.js").stat().st_mtime_ns
            return template_page(
                "Run",
                "control.html",
                read_only=read_only,
                analyze_kinds=ANALYZE_KINDS,
                report_kinds=REPORT_KINDS,
                status=runner.status(),
                scripts=f"<script defer src='/static/folder-picker.js?v={folder_picker_version}'></script>",
                active_path="control",
            )

        @app.get("/fragments/control", response_class=HTMLResponse)
        def control_status_fragment():
            return control_fragment()

        @app.post("/control/scan", response_class=HTMLResponse)
        def control_scan(
            path: Annotated[str, Form()], x_csrf_token: str | None = Header(default=None)
        ):
            guard(x_csrf_token)
            if not Path(path).is_dir():
                raise HTTPException(422, "path must be an existing directory")
            if runner.submit("quickstart", source=path) == "busy":
                raise HTTPException(409, "an operation is already running")
            return control_fragment(job_started=True)

        @app.post("/control/analyze", response_class=HTMLResponse)
        def control_analyze(
            kind: Annotated[str, Form()], x_csrf_token: str | None = Header(default=None)
        ):
            guard(x_csrf_token)
            if kind != "all" and kind not in ANALYZE_KINDS:
                raise HTTPException(422, "invalid analyze kind")
            if runner.submit("analyze", kind=kind) == "busy":
                raise HTTPException(409, "an operation is already running")
            return control_fragment(job_started=True)

        @app.post("/control/classify", response_class=HTMLResponse)
        def control_classify(x_csrf_token: str | None = Header(default=None)):
            guard(x_csrf_token)
            if runner.submit("classify") == "busy":
                raise HTTPException(409, "an operation is already running")
            return control_fragment(job_started=True)

        @app.post("/control/report", response_class=HTMLResponse)
        def control_report(
            kind: Annotated[str, Form()], x_csrf_token: str | None = Header(default=None)
        ):
            guard(x_csrf_token)
            if kind != "all" and kind not in REPORT_KINDS:
                raise HTTPException(422, "invalid report kind")
            if runner.submit("report", kind=kind) == "busy":
                raise HTTPException(409, "an operation is already running")
            return control_fragment(job_started=True)

    @app.get("/graph", response_class=HTMLResponse)
    def graph_page():
        body = "<p>Bounded Cytoscape.js projection. Click a node to inspect it; double-click to progressively expand its local neighborhood. Structured explorers remain authoritative.</p><div class='graph-controls'><label>Projection <select id='graph-projection'><option>universe</option><option>duplicate</option><option>content</option><option>backup-lineage</option><option>project</option><option>document-family</option><option>image-cluster</option></select></label><label>Layout <select id='graph-layout'><option>concentric</option><option>breadthfirst</option><option>grid</option><option>cose</option></select></label><label>Confidence <input id='graph-confidence' type='number' min='0' max='1' step='.05' value='.7'></label><input id='graph-search' placeholder='Search nodes'><button id='graph-load'>Load</button><button id='graph-export'>Export PNG</button><span id='graph-status'></span></div><div id='cy' role='application' aria-label='Storage relationship graph'></div><pre id='graph-detail'></pre>"
        return page(
            "Graph",
            body,
            scripts="<script defer src='/static/vendor/cytoscape.min.js'></script><script defer src='/static/app.js'></script>",
            active_path="graph",
        )

    @app.get("/manifests", response_class=HTMLResponse)
    def manifests(limit: int = Query(100, ge=1, le=500)):
        return explorer(
            "manifests",
            "Manifest center",
            "SELECT id,name,status,base_scan_run_id,analysis_snapshot_id,updated_at FROM review_sessions ORDER BY updated_at DESC LIMIT ?",
            ["id", "name", "status", "base_scan_run_id", "analysis_snapshot_id", "updated_at"],
            limit,
        )

    @app.get("/search", response_class=HTMLResponse)
    def search(q: str = Query(..., min_length=1, max_length=500), limit: int = Query(100, ge=1, le=500)):
        escaped_prefix = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        rows = reader.fetch_all(
            "SELECT id,name,relative_path,size_bytes,modified_at FROM filesystem_entries "
            "WHERE relative_path LIKE ? ESCAPE '\\' OR name LIKE ? ESCAPE '\\' "
            "ORDER BY relative_path LIMIT ?",
            (f"{escaped_prefix}%", f"{escaped_prefix}%", limit),
        )
        body = (
            f"<p>{thousands(len(rows))} matches for <strong>{escape(q)}</strong>. "
            "Search is prefix-based and read-only.</p>"
            + rows_table(
                rows,
                ["id", "name", "relative_path", "size_bytes", "modified_at"],
                "No paths begin with that search. Try a shorter prefix.",
            )
        )
        return page("Search", body, active_path="search")

    @app.get("/api/overview")
    def api_overview():
        with database.read_connection() as conn:
            return {
                "entries": conn.execute("SELECT COUNT(*) n FROM filesystem_entries").fetchone()[
                    "n"
                ],
                "content_objects": conn.execute(
                    "SELECT COUNT(*) n FROM content_objects"
                ).fetchone()["n"],
                "jobs": conn.execute(
                    "SELECT COUNT(*) n FROM jobs WHERE status IN ('PENDING','RUNNING','PAUSED','CANCELLING')"
                ).fetchone()["n"],
            }

    @app.get("/api/review")
    def api_review(
        limit: int = Query(100, ge=1, le=500),
        after_id: int = Query(0, ge=0),
        classification: str | None = None,
        extension: str | None = None,
        minimum_size: int | None = Query(None, ge=0),
        maximum_size: int | None = Query(None, ge=0),
        stale: bool | None = None,
    ):
        return [
            dict(row)
            for row in review_rows(
                limit, after_id, classification, extension, minimum_size, maximum_size, stale
            )
        ]

    @app.get("/api/entry/{entry_id}")
    def entry_detail(entry_id: int):
        row = reader.fetch_one(
            "SELECT e.*,s.full_hash,s.hash_status,c.classification,c.confidence,c.explanation FROM filesystem_entries e LEFT JOIN file_signatures s ON s.entry_id=e.id LEFT JOIN classifications c ON c.entry_id=e.id WHERE e.id=?",
            (entry_id,),
        )
        if not row:
            raise HTTPException(404, "entry not found")
        return dict(row)

    @app.post("/api/review/decision")
    def api_decision(
        session_id: Annotated[int, Query(ge=1)],
        target_type: str,
        target_id: Annotated[int, Query(ge=1)],
        decision: str,
        x_csrf_token: str | None = Header(default=None),
    ):
        guard(x_csrf_token)
        if decision not in {
            "APPROVE_FOR_REVIEW",
            "REJECT_RECOMMENDATION",
            "DEFER",
            "MARK_KEEP",
            "MARK_PROTECTED",
            "NEEDS_MORE_ANALYSIS",
        }:
            raise HTTPException(422, "invalid decision")
        return {
            "decision_id": record_decision(
                database, session_id, target_type, target_id, decision, source="dashboard"
            )
        }

    @app.post("/api/review/canonical")
    def canonical_override(
        session_id: Annotated[int, Query(ge=1)],
        group_id: Annotated[int, Query(ge=1)],
        entry_id: Annotated[int, Query(ge=1)],
        x_csrf_token: str | None = Header(default=None),
    ):
        guard(x_csrf_token)
        from ..review.canonical import override_canonical

        override_canonical(database, session_id, group_id, entry_id)
        return {"group_id": group_id, "canonical_entry_id": entry_id}

    @app.post("/api/review/bulk-duplicate-decision")
    def bulk_duplicate_decision(
        session_id: Annotated[int, Query(ge=1)],
        group_ids: list[int],
        decision: str,
        x_csrf_token: str | None = Header(default=None),
    ):
        guard(x_csrf_token)
        if (
            not group_ids
            or len(group_ids) > 250
            or decision not in {"DEFER", "MARK_KEEP", "NEEDS_MORE_ANALYSIS"}
        ):
            raise HTTPException(422, "invalid safe bulk duplicate decision")
        return {
            "decision_ids": [
                record_decision(
                    database,
                    session_id,
                    "DUPLICATE_GROUP",
                    group_id,
                    decision,
                    source="dashboard-bulk",
                )
                for group_id in sorted(set(group_ids))
            ]
        }

    @app.get("/api/duplicates")
    def api_duplicates(limit: int = Query(100, ge=1, le=500), after_id: int = Query(0, ge=0)):
        return [
            dict(row)
            for row in reader.fetch_all(
                "SELECT id,full_hash,member_count,size_bytes,canonical_entry_id FROM exact_duplicate_groups WHERE id>? ORDER BY id LIMIT ?",
                (after_id, limit),
            )
        ]

    @app.get("/api/duplicates/{group_id}")
    def duplicate_detail(group_id: Annotated[int, ApiPath(ge=1)]):
        group = reader.fetch_one("SELECT * FROM exact_duplicate_groups WHERE id=?", (group_id,))
        if not group:
            raise HTTPException(404, "duplicate group not found")
        members = reader.fetch_all(
            "SELECT e.id,e.name,e.relative_path,e.absolute_path,e.modified_at,m.is_canonical,m.readable FROM exact_duplicate_members m JOIN filesystem_entries e ON e.id=m.entry_id WHERE m.group_id=? ORDER BY m.is_canonical DESC,e.relative_path",
            (group_id,),
        )
        return {"group": dict(group), "members": [dict(member) for member in members]}

    @app.get("/api/jobs")
    def api_jobs(limit: int = Query(100, ge=1, le=500)):
        return [
            dict(row)
            for row in reader.fetch_all(
                "SELECT id,job_type,status,processed_count,total_estimate,current_item,updated_at FROM jobs ORDER BY id DESC LIMIT ?",
                (limit,),
            )
        ]

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(
        job_id: Annotated[int, ApiPath(ge=1)], x_csrf_token: str | None = Header(default=None)
    ):
        guard(x_csrf_token)
        from ..jobs import request_cancel

        request_cancel(database, job_id)
        return {"job_id": job_id, "status": "CANCELLING"}

    @app.post("/api/jobs/{job_id}/pause")
    def pause_job(
        job_id: Annotated[int, ApiPath(ge=1)], x_csrf_token: str | None = Header(default=None)
    ):
        guard(x_csrf_token)
        from ..jobs import request_pause

        request_pause(database, job_id)
        return {"job_id": job_id, "status": "PAUSING"}

    @app.get("/api/manifests/{session_id}")
    def api_manifest(session_id: int):
        session = reader.fetch_one(
            "SELECT id,status,analysis_snapshot_id FROM review_sessions WHERE id=?", (session_id,)
        )
        if not session:
            raise HTTPException(404, "review session not found")
        return {
            "session_id": session_id,
            "status": session["status"],
            "snapshot_id": session["analysis_snapshot_id"],
            "records": decision_manifest_records(session_id),
            "movement": "CLI only",
            "dry_run_command": f"housekeeper validate-manifest workspace/manifests/session-{session_id}.jsonl && housekeeper move-to-review workspace/manifests/session-{session_id}.jsonl REVIEW_ROOT --dry-run",
        }

    @app.post("/api/manifests/{session_id}/export")
    def export_manifest(
        session_id: Annotated[int, ApiPath(ge=1)], x_csrf_token: str | None = Header(default=None)
    ):
        guard(x_csrf_token)
        from fastapi.responses import Response
        from ..review.decisions import export_snapshot, validate_session

        errors = validate_session(database, session_id)
        if errors:
            raise HTTPException(409, "; ".join(errors))
        body = "".join(
            json.dumps(record, sort_keys=True) + "\n"
            for record in decision_manifest_records(session_id)
        )
        manifest_hash = hashlib.sha256(body.encode()).hexdigest()
        export_snapshot(
            database,
            session_id,
            {"manifest_hash": manifest_hash, "format": "jsonl", "generated_by": "dashboard"},
        )
        database.connect().execute(
            "UPDATE review_sessions SET status='EXPORTED',updated_at=CURRENT_TIMESTAMP WHERE id=?",
            (session_id,),
        )
        database.connect().commit()
        return Response(
            body,
            media_type="application/x-ndjson",
            headers={
                "Content-Disposition": f'attachment; filename="housekeeper-session-{session_id}.jsonl"'
            },
        )

    @app.get("/api/graph/projection")
    def graph(
        projection_type: str = "universe",
        root_type: str | None = None,
        root_id: int | None = Query(None, ge=1),
        depth: int = Query(1, ge=1, le=5),
        max_nodes: int = Query(500, ge=1, le=5000),
        max_edges: int = Query(2000, ge=1, le=20000),
        minimum_confidence: float = Query(0.7, ge=0, le=1),
        aggregation_level: str = "auto",
        include_types: tuple[str, ...] = Query(()),
        exclude_types: tuple[str, ...] = Query(()),
    ):
        try:
            return build_projection(
                database,
                projection_type,
                root_id,
                root_type,
                depth,
                max_nodes,
                max_edges,
                minimum_confidence,
                aggregation_level,
                include_types,
                exclude_types,
            )
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get("/api/csrf")
    def csrf():
        return {"token": csrf_token}

    return app
