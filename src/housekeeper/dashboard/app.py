"""Local, bounded dashboard.  It can record review decisions but never moves data."""

from html import escape
import hashlib
import json
from pathlib import Path
import secrets
from typing import Annotated

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
    runner = None
    if config is not None:
        from .runner import OperationRunner

        runner = OperationRunner(config)
    navigation: list[tuple[str, str]] = [("", "Overview")]
    if runner is not None:
        navigation.append(("control", "Run"))
    navigation.extend(
        [
            ("review", "Review"),
            ("duplicates", "Duplicates"),
            ("advanced-duplicates", "Advanced dupes"),
            ("chunk-overlap", "Chunk overlap"),
            ("derivations", "Derivations"),
            ("backups", "Backups"),
            ("documents", "Documents"),
            ("images", "Images"),
            ("projects", "Projects"),
            ("events", "Events"),
            ("record-series", "Record series"),
            ("preservation", "Preservation"),
            ("learning", "Learning"),
            ("jobs", "Jobs"),
            ("graph", "Graph"),
            ("manifests", "Manifests"),
        ]
    )

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
                app_css_version=(static_dir / "app.css").stat().st_mtime_ns,
                theme_switch_version=(static_dir / "theme-switch.js").stat().st_mtime_ns,
                csrf_token=csrf_token,
                navigation=navigation,
            )
        )

    def template_page(
        title: str, template_name: str, *, scripts: str = "", **context
    ) -> HTMLResponse:
        body = templates.get_template(template_name).render(**context)
        return page(title, body, scripts=scripts)

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

    def progress_cell(row) -> str:
        """Render a job row's progress: a real percentage when a total is known, otherwise an
        indeterminate bar with live counters. Never fabricates an ETA for an unknown total."""
        processed = row["processed_count"] or 0
        total = row["total_estimate"]
        rate = throughput(processed, seconds_since(row["started_at"]))
        danger = " class='hk-progress--danger'" if row["status"] == "FAILED" else ""
        if total:
            pct = min(100, int(processed * 100 / total))
            eta = eta_seconds(processed, total, rate)
            eta_html = f" · ETA {format_duration(eta)}" if eta is not None else ""
            return (
                f"<progress{danger} value='{processed}' max='{total}'></progress> "
                f"{pct}% {processed:,}/{total:,} · {rate:,.1f}/s{eta_html}"
            )
        current = f" · {escape(str(row['current_item']))}" if row["current_item"] else ""
        return f"<progress{danger}></progress> {processed:,} processed · {rate:,.1f}/s{current}"

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
                else f"<td>{escape(str(row[heading] if row[heading] is not None else ''))}</td>"
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

    def linked_rows_table(rows, headings: list[str], id_key: str, href_prefix: str) -> str:
        """A rows_table with a trailing detail-link column.

        The href is built exclusively from ``int(row[id_key])`` so no row value can inject markup.
        """
        header = "".join(f"<th>{escape(h)}</th>" for h in [*headings, "detail"])
        body = ""
        for row in rows:
            cells = "".join(
                f"<td>{escape(str(row[h] if h in row.keys() and row[h] is not None else ''))}</td>"
                for h in headings
            )
            body += f"<tr>{cells}<td><a href='{href_prefix}/{int(row[id_key])}'>open</a></td></tr>"
        return f"<table><thead><tr>{header}</tr></thead><tbody>{body or '<tr><td colspan=99>No results</td></tr>'}</tbody></table>"

    def directory_card(entry_id: int) -> dict | None:
        entry = database.fetch_one(
            "SELECT id,name,relative_path,source_root_id FROM filesystem_entries WHERE id=? AND entry_type='directory'",
            (entry_id,),
        )
        if not entry:
            return None
        summary = database.fetch_one(
            "SELECT recursive_file_count,recursive_directory_count,recursive_size_bytes,"
            "unique_full_hash_count,duplicate_file_count,earliest_modified_at,latest_modified_at "
            "FROM directory_summaries WHERE entry_id=?",
            (entry_id,),
        )
        return {**dict(entry), **(dict(summary) if summary else {})}

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
        rows = database.fetch_all(
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
        )

    def _representative_entry(content_object_id: int) -> dict | None:
        row = database.fetch_one(
            "SELECT e.name,e.relative_path,e.modified_at FROM entry_content_links l "
            "JOIN filesystem_entries e ON e.id=l.entry_id WHERE l.content_object_id=? ORDER BY e.id LIMIT 1",
            (content_object_id,),
        )
        return dict(row) if row else None

    @app.get("/derivations/{content_object_id}", response_class=HTMLResponse)
    def derivation_timeline(content_object_id: Annotated[int, ApiPath(ge=1)]):
        relationships = database.fetch_all(
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
        rows = database.fetch_all(
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
        )

    @app.get("/backups/{relationship_id}", response_class=HTMLResponse)
    def backup_compare(relationship_id: Annotated[int, ApiPath(ge=1)]):
        relationship = database.fetch_one(
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
        groups = database.fetch_all(
            "SELECT g.id,g.group_key,COUNT(m.content_object_id) member_count FROM relationship_groups g"
            " JOIN relationship_group_members m ON m.group_id=g.id"
            " WHERE g.group_type='IMAGE_SIMILARITY' GROUP BY g.id ORDER BY member_count DESC,g.id LIMIT ?",
            (limit,),
        )
        artifacts = database.fetch_all(
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
        )

    def _contact_sheet_file(group_id: int) -> Path | None:
        if contact_sheet_dir is None:
            return None
        # The path is derived only from the validated integer id — never from request text.
        candidate = contact_sheet_dir / f"group_{group_id}.jpg"
        return candidate if candidate.is_file() else None

    @app.get("/images/{group_id}", response_class=HTMLResponse)
    def image_group_detail(group_id: Annotated[int, ApiPath(ge=1)]):
        group = database.fetch_one(
            "SELECT id,group_key,evidence_json,created_at FROM relationship_groups"
            " WHERE id=? AND group_type='IMAGE_SIMILARITY'",
            (group_id,),
        )
        if not group:
            raise HTTPException(404, "image similarity group not found")
        members = database.fetch_all(
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
        return page(
            "Jobs",
            f"<section hx-get='/fragments/jobs?limit={limit}' hx-trigger='load, every 3s'>Loading…</section>",
        )

    @app.get("/fragments/jobs", response_class=HTMLResponse)
    def jobs_fragment(limit: int = Query(100, ge=1, le=500)):
        rows = database.fetch_all(
            "SELECT id,job_type,status,processed_count,total_estimate,success_count,skip_count,error_count,current_item,started_at,updated_at FROM jobs ORDER BY id DESC LIMIT ?",
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
            "SELECT id,job_type,status,processed_count,total_estimate,success_count,skip_count,error_count,current_item,started_at,updated_at FROM jobs WHERE id=?",
            (job_id,),
        )
        if not row:
            raise HTTPException(404, "job not found")
        table = jobs_table([row])
        return HTMLResponse(table.split("<tbody>", 1)[1].split("</tbody>", 1)[0])

    if runner is not None:
        from .runner import ANALYZE_KINDS, REPORT_KINDS

        def control_fragment() -> HTMLResponse:
            return HTMLResponse(
                templates.get_template("fragments/control.html").render(
                    status=runner.status(),
                    read_only=read_only,
                    analyze_kinds=ANALYZE_KINDS,
                    report_kinds=REPORT_KINDS,
                )
            )

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
            return control_fragment()

        @app.post("/control/analyze", response_class=HTMLResponse)
        def control_analyze(
            kind: Annotated[str, Form()], x_csrf_token: str | None = Header(default=None)
        ):
            guard(x_csrf_token)
            if kind != "all" and kind not in ANALYZE_KINDS:
                raise HTTPException(422, "invalid analyze kind")
            if runner.submit("analyze", kind=kind) == "busy":
                raise HTTPException(409, "an operation is already running")
            return control_fragment()

        @app.post("/control/classify", response_class=HTMLResponse)
        def control_classify(x_csrf_token: str | None = Header(default=None)):
            guard(x_csrf_token)
            if runner.submit("classify") == "busy":
                raise HTTPException(409, "an operation is already running")
            return control_fragment()

        @app.post("/control/report", response_class=HTMLResponse)
        def control_report(
            kind: Annotated[str, Form()], x_csrf_token: str | None = Header(default=None)
        ):
            guard(x_csrf_token)
            if kind != "all" and kind not in REPORT_KINDS:
                raise HTTPException(422, "invalid report kind")
            if runner.submit("report", kind=kind) == "busy":
                raise HTTPException(409, "an operation is already running")
            return control_fragment()

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
