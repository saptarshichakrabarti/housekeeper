"""Read-only dashboard queries, intentionally separate from HTTP rendering."""

from __future__ import annotations

from .filters import ReviewFilter
from .view_models import Chart, Metric, OverviewViewModel, ReviewRow


class DashboardService:
    def __init__(self, database):
        self.database = database

    def overview(self) -> OverviewViewModel:
        stats = self.database.database_stats()
        query = self.database.fetch_one
        metrics = (
            Metric("Sources", int(query("SELECT COUNT(*) n FROM source_roots")["n"])),
            Metric("Entries", int(stats["entries"])),
            Metric("Content objects", int(stats["content_objects"])),
            Metric("Artifacts", int(stats["analysis_artifacts"])),
            Metric(
                "Duplicate groups", int(query("SELECT COUNT(*) n FROM exact_duplicate_groups")["n"])
            ),
            Metric(
                "Logical bytes",
                int(
                    query(
                        "SELECT COALESCE(SUM(size_bytes),0) n FROM filesystem_entries WHERE entry_type='file'"
                    )["n"]
                ),
            ),
            Metric(
                "Unique bytes",
                int(query("SELECT COALESCE(SUM(size_bytes),0) n FROM content_objects")["n"]),
            ),
            Metric(
                "Protected",
                int(
                    query(
                        "SELECT COUNT(*) n FROM classifications WHERE classification='PROTECTED'"
                    )["n"]
                ),
            ),
            Metric(
                "Active jobs",
                int(
                    query(
                        "SELECT COUNT(*) n FROM jobs WHERE status IN ('PENDING','RUNNING','PAUSED','PAUSING','CANCELLING')"
                    )["n"]
                ),
            ),
        )

        def chart(title: str, columns: tuple[str, ...], sql: str) -> Chart:
            rows = self.database.fetch_all(sql)
            return Chart(
                title,
                columns,
                tuple(
                    tuple(str(row[column] if row[column] is not None else "") for column in columns)
                    for row in rows
                ),
            )

        charts = (
            chart(
                "File types",
                ("suffix", "files", "bytes"),
                "SELECT COALESCE(suffix,'(none)') suffix,COUNT(*) files,SUM(size_bytes) bytes FROM filesystem_entries WHERE entry_type='file' GROUP BY suffix ORDER BY bytes DESC LIMIT 20",
            ),
            chart(
                "Classification bytes",
                ("classification", "files", "bytes"),
                "SELECT COALESCE(c.classification,'UNCLASSIFIED') classification,COUNT(*) files,SUM(e.size_bytes) bytes FROM filesystem_entries e LEFT JOIN classifications c ON c.entry_id=e.id WHERE e.entry_type='file' GROUP BY c.classification ORDER BY bytes DESC LIMIT 20",
            ),
            chart(
                "Top-level directories",
                ("top_level", "files", "bytes"),
                "SELECT CASE WHEN instr(relative_path,'/')=0 THEN relative_path ELSE substr(relative_path,1,instr(relative_path,'/')-1) END top_level,COUNT(*) files,SUM(size_bytes) bytes FROM filesystem_entries WHERE entry_type='file' GROUP BY top_level ORDER BY bytes DESC LIMIT 20",
            ),
            chart(
                "Scan history",
                ("id", "status", "files_seen", "bytes_seen", "completed_at"),
                "SELECT id,status,files_seen,bytes_seen,COALESCE(completed_at,'') completed_at FROM scan_runs ORDER BY id DESC LIMIT 20",
            ),
            chart(
                "Analyzer completion",
                ("analyzer_name", "status", "count"),
                "SELECT analyzer_name,status,COUNT(*) count FROM analysis_artifacts GROUP BY analyzer_name,status ORDER BY analyzer_name,status",
            ),
        )
        return OverviewViewModel(str(stats["integrity"]), metrics, charts)

    def review_rows(self, filters: ReviewFilter, limit: int, after_id: int) -> list[ReviewRow]:
        where, params = filters.where_clause()
        rows = self.database.fetch_all(
            f"""SELECT e.id,e.name,e.relative_path,e.source_root,e.size_bytes,e.modified_at,c.classification,c.confidence,
                COALESCE(d.decision,'') decision,COALESCE(c.reason_codes_json,'[]') reason_codes,COALESCE(d.user_note,'') notes,COALESCE(d.stale,0) stale,
                EXISTS(SELECT 1 FROM exact_duplicate_members dm WHERE dm.entry_id=e.id) duplicate_member,
                EXISTS(SELECT 1 FROM projects p WHERE p.root_entry_id=e.id) project_member
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
            )
            for row in rows
        ]
