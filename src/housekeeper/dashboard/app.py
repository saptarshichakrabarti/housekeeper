"""Local, bounded dashboard.  It can record review decisions but never moves data."""

from html import escape
import hashlib
import json
from pathlib import Path
import secrets
from typing import Annotated


def create_app(database, read_only: bool = False):
    try:
        from fastapi import FastAPI, Form, Header, HTTPException, Path as ApiPath, Query
        from fastapi.responses import HTMLResponse
        from fastapi.staticfiles import StaticFiles
    except ImportError as exc:
        raise RuntimeError(
            "Install the dashboard extra: pip install 'drive-housekeeper[dashboard]'"
        ) from exc
    from ..graph.builder import build_projection
    from ..review.decisions import record_decision
    from .filters import ReviewFilter
    from .services import DashboardService
    from jinja2 import Environment, FileSystemLoader, select_autoescape
    from markupsafe import Markup

    csrf_token = secrets.token_urlsafe(24)
    static_dir = Path(__file__).with_name("static")
    templates = Environment(
        loader=FileSystemLoader(Path(__file__).with_name("templates")),
        autoescape=select_autoescape(["html", "xml"]),
    )
    app = FastAPI(title="drive_housekeeper", docs_url=None, redoc_url=None)
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    service = DashboardService(database)

    def guard(token: str | None) -> None:
        if read_only:
            raise HTTPException(403, "dashboard is read-only")
        if token != csrf_token:
            raise HTTPException(403, "CSRF validation failed")

    def page(title: str, body: str, *, scripts: str = "") -> HTMLResponse:
        return HTMLResponse(
            templates.get_template("base.html").render(
                title=title,
                body=Markup(body),
                scripts=Markup(scripts),
                csrf_token=csrf_token,
                navigation=(
                    ("", "Overview"),
                    ("review", "Review"),
                    ("duplicates", "Duplicates"),
                    ("backups", "Backups"),
                    ("documents", "Documents"),
                    ("images", "Images"),
                    ("projects", "Projects"),
                    ("jobs", "Jobs"),
                    ("graph", "Graph"),
                    ("manifests", "Manifests"),
                ),
            )
        )

    def template_page(title: str, template_name: str, **context) -> HTMLResponse:
        body = templates.get_template(template_name).render(**context)
        return page(title, body)

    def rows_table(rows, headings: list[str]) -> str:
        header = "".join(f"<th>{escape(h)}</th>" for h in headings)
        body = "".join(
            "<tr>"
            + "".join(
                f"<td>{escape(str(row[h] if h in row.keys() and row[h] is not None else ''))}</td>"
                for h in headings
            )
            + "</tr>"
            for row in rows
        )
        return f"<table><thead><tr>{header}</tr></thead><tbody>{body or '<tr><td colspan=99>No results</td></tr>'}</tbody></table>"

    def jobs_table(rows) -> str:
        headings = [
            "id",
            "job_type",
            "status",
            "processed_count",
            "total_estimate",
            "success_count",
            "skip_count",
            "error_count",
            "current_item",
            "updated_at",
        ]
        header = "".join(f"<th>{escape(heading)}</th>" for heading in [*headings, "controls"])
        body = ""
        for row in rows:
            cells = "".join(
                f"<td>{escape(str(row[heading] if row[heading] is not None else ''))}</td>"
                for heading in headings
            )
            controls = ""
            if row["status"] in {"PENDING", "RUNNING"}:
                controls = f"<button hx-post='/fragments/jobs/{row['id']}/control?action=pause' hx-target='closest tr'>Pause</button> <button hx-post='/fragments/jobs/{row['id']}/control?action=cancel' hx-target='closest tr'>Cancel</button>"
            body += f"<tr>{cells}<td>{controls}</td></tr>"
        return f"<table><thead><tr>{header}</tr></thead><tbody>{body or '<tr><td colspan=99>No results</td></tr>'}</tbody></table>"

    def decision_manifest_records(session_id: int) -> list[dict[str, object]]:
        rows = database.fetch_all(
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
        return database.fetch_all(
            f"SELECT e.id,e.name,e.relative_path,e.size_bytes,e.modified_at,c.classification,c.confidence FROM filesystem_entries e LEFT JOIN classifications c ON c.entry_id=e.id WHERE {where} ORDER BY e.id LIMIT ?",
            tuple(params),
        )

    @app.middleware("http")
    async def security_headers(request, call_next):
        response = await call_next(request)
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:"
        )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.get("/", response_class=HTMLResponse)
    def overview():
        return template_page("Housekeeper overview", "overview.html", model=service.overview())

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
        query = f"?limit={limit}&after_id={after_id}" + (
            f"&classification={classification}" if classification else ""
        )
        next_id = rows[-1].entry_id if rows else None
        return template_page(
            "Review queue",
            "review.html",
            rows=rows,
            next_id=next_id,
            next_url=f"/review?limit={limit}&after_id={next_id}" if next_id else "",
            active_filter=query,
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
        entry = database.fetch_one(
            """SELECT e.id,e.name,e.relative_path,e.size_bytes,s.full_hash,s.hash_status,c.classification
               FROM filesystem_entries e LEFT JOIN file_signatures s ON s.entry_id=e.id
               LEFT JOIN classifications c ON c.entry_id=e.id WHERE e.id=?""",
            (entry_id,),
        )
        if not entry:
            raise HTTPException(404, "entry not found")
        artifacts = database.fetch_all(
            """SELECT a.analyzer_name,a.status,a.completed_at FROM entry_content_links l
               JOIN analysis_artifacts a ON a.content_object_id=l.content_object_id
               WHERE l.entry_id=? ORDER BY a.completed_at DESC""",
            (entry_id,),
        )
        return HTMLResponse(
            templates.get_template("fragments/entry_detail.html").render(
                entry=dict(entry), artifacts=[dict(artifact) for artifact in artifacts]
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
        rows = database.fetch_all(query, (limit,))
        return page(
            title, f"<p>Bounded explorer; results are read-only.</p>{rows_table(rows, headings)}"
        )

    @app.get("/duplicates", response_class=HTMLResponse)
    def duplicates(limit: int = Query(100, ge=1, le=500)):
        return explorer(
            "duplicates",
            "Duplicate explorer",
            "SELECT id,full_hash,member_count,size_bytes,canonical_entry_id FROM exact_duplicate_groups ORDER BY member_count DESC,id LIMIT ?",
            ["id", "full_hash", "member_count", "size_bytes", "canonical_entry_id"],
            limit,
        )

    @app.get("/fragments/duplicates/{group_id}", response_class=HTMLResponse)
    def duplicate_detail_fragment(group_id: Annotated[int, ApiPath(ge=1)]):
        group = database.fetch_one("SELECT * FROM exact_duplicate_groups WHERE id=?", (group_id,))
        if not group:
            raise HTTPException(404, "duplicate group not found")
        members = database.fetch_all(
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

    @app.get("/backups", response_class=HTMLResponse)
    def backups(limit: int = Query(100, ge=1, le=500)):
        return explorer(
            "backups",
            "Backup comparison",
            "SELECT source_type,source_id,target_type,target_id,relationship_type,confidence FROM relationships WHERE relationship_type LIKE '%BACKUP%' ORDER BY confidence DESC,id LIMIT ?",
            [
                "source_type",
                "source_id",
                "target_type",
                "target_id",
                "relationship_type",
                "confidence",
            ],
            limit,
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
        return explorer(
            "images",
            "Image-similarity explorer",
            "SELECT content_object_id,analyzer_version,status,completed_at,error_code FROM analysis_artifacts WHERE analyzer_name='images' ORDER BY completed_at DESC LIMIT ?",
            ["content_object_id", "analyzer_version", "status", "completed_at", "error_code"],
            limit,
        )

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
        return page(
            "Jobs",
            f"<section hx-get='/fragments/jobs?limit={limit}' hx-trigger='load, every 3s'>Loading…</section>",
        )

    @app.get("/fragments/jobs", response_class=HTMLResponse)
    def jobs_fragment(limit: int = Query(100, ge=1, le=500)):
        rows = database.fetch_all(
            "SELECT id,job_type,status,processed_count,total_estimate,success_count,skip_count,error_count,current_item,updated_at FROM jobs ORDER BY id DESC LIMIT ?",
            (limit,),
        )
        return HTMLResponse(jobs_table(rows))

    @app.post("/fragments/jobs/{job_id}/control", response_class=HTMLResponse)
    def job_control_fragment(
        job_id: Annotated[int, ApiPath(ge=1)],
        action: str = Query(...),
        x_csrf_token: str | None = Header(default=None),
    ):
        guard(x_csrf_token)
        if action == "pause":
            from ..jobs import request_pause

            request_pause(database, job_id)
        elif action == "cancel":
            from ..jobs import request_cancel

            request_cancel(database, job_id)
        else:
            raise HTTPException(422, "invalid job control")
        row = database.fetch_one(
            "SELECT id,job_type,status,processed_count,total_estimate,success_count,skip_count,error_count,current_item,updated_at FROM jobs WHERE id=?",
            (job_id,),
        )
        if not row:
            raise HTTPException(404, "job not found")
        table = jobs_table([row])
        return HTMLResponse(table.split("<tbody>", 1)[1].split("</tbody>", 1)[0])

    @app.get("/graph", response_class=HTMLResponse)
    def graph_page():
        body = "<p>Bounded Cytoscape.js projection. Click a node to inspect it; double-click to progressively expand its local neighborhood. Structured explorers remain authoritative.</p><div class='graph-controls'><label>Projection <select id='graph-projection'><option>universe</option><option>duplicate</option><option>content</option><option>backup-lineage</option><option>project</option><option>document-family</option><option>image-cluster</option></select></label><label>Layout <select id='graph-layout'><option>concentric</option><option>breadthfirst</option><option>grid</option><option>cose</option></select></label><label>Confidence <input id='graph-confidence' type='number' min='0' max='1' step='.05' value='.7'></label><input id='graph-search' placeholder='Search nodes'><button id='graph-load'>Load</button><button id='graph-export'>Export PNG</button><span id='graph-status'></span></div><div id='cy' role='application' aria-label='Storage relationship graph'></div><pre id='graph-detail'></pre>"
        return page(
            "Graph",
            body,
            scripts="<script defer src='/static/vendor/cytoscape.min.js'></script><script defer src='/static/app.js'></script>",
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
        row = database.fetch_one(
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
            for row in database.fetch_all(
                "SELECT id,full_hash,member_count,size_bytes,canonical_entry_id FROM exact_duplicate_groups WHERE id>? ORDER BY id LIMIT ?",
                (after_id, limit),
            )
        ]

    @app.get("/api/duplicates/{group_id}")
    def duplicate_detail(group_id: Annotated[int, ApiPath(ge=1)]):
        group = database.fetch_one("SELECT * FROM exact_duplicate_groups WHERE id=?", (group_id,))
        if not group:
            raise HTTPException(404, "duplicate group not found")
        members = database.fetch_all(
            "SELECT e.id,e.name,e.relative_path,e.absolute_path,e.modified_at,m.is_canonical,m.readable FROM exact_duplicate_members m JOIN filesystem_entries e ON e.id=m.entry_id WHERE m.group_id=? ORDER BY m.is_canonical DESC,e.relative_path",
            (group_id,),
        )
        return {"group": dict(group), "members": [dict(member) for member in members]}

    @app.get("/api/jobs")
    def api_jobs(limit: int = Query(100, ge=1, le=500)):
        return [
            dict(row)
            for row in database.fetch_all(
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
        session = database.fetch_one(
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
