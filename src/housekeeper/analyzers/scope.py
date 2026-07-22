"""Shared, parameterized analyzer scope resolution."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class AnalyzerScope:
    under: str | None = None
    source_id: int | None = None
    scan_run_id: int | None = None
    detected_mime: str | None = None
    extensions: frozenset[str] = frozenset()
    size_min: int | None = None
    size_max: int | None = None
    older_than: str | None = None
    newer_than: str | None = None
    classification: str | None = None
    only_unique: bool = False
    only_duplicate_candidates: bool = False
    content_object_ids: frozenset[int] = frozenset()
    current_inventory: bool = False

    def entry_query(self, entry_type: str = "file") -> tuple[str, tuple[object, ...]]:
        clauses = ["e.entry_type=?"]
        params: list[object] = [entry_type]
        if self.current_inventory:
            # Restrict to the latest COMPLETE scan run per source root, so two snapshots of the
            # same physical file across re-scans are never grouped as duplicates of each other.
            clauses.append(
                "e.scan_run_id IN (SELECT MAX(id) FROM scan_runs WHERE status='COMPLETE' GROUP BY source_root_fingerprint)"
            )
        if self.source_id is not None:
            clauses.append("e.source_root_id=?")
            params.append(self.source_id)
        if self.scan_run_id is not None:
            clauses.append("e.scan_run_id=?")
            params.append(self.scan_run_id)
        if self.under:
            clauses.append("e.absolute_path LIKE ?")
            params.append(self.under.rstrip("/") + "/%")
        if self.extensions:
            clauses.append("e.suffix IN (" + ",".join("?" for _ in self.extensions) + ")")
            params.extend(sorted(self.extensions))
        if self.size_min is not None:
            clauses.append("e.size_bytes>=?")
            params.append(self.size_min)
        if self.size_max is not None:
            clauses.append("e.size_bytes<=?")
            params.append(self.size_max)
        if self.older_than:
            clauses.append("e.modified_at<?")
            params.append(datetime.fromisoformat(self.older_than).timestamp())
        if self.newer_than:
            clauses.append("e.modified_at>?")
            params.append(datetime.fromisoformat(self.newer_than).timestamp())
        if self.classification:
            clauses.append("c.classification=?")
            params.append(self.classification)
        if self.detected_mime:
            clauses.append("s.detected_mime=?")
            params.append(self.detected_mime)
        if self.content_object_ids:
            clauses.append(
                "l.content_object_id IN (" + ",".join("?" for _ in self.content_object_ids) + ")"
            )
            params.extend(sorted(self.content_object_ids))
        if self.only_unique:
            clauses.append(
                "(SELECT COUNT(*) FROM entry_content_links same WHERE same.content_object_id=l.content_object_id AND same.link_status='VERIFIED')=1"
            )
        if self.only_duplicate_candidates:
            clauses.append(
                "(SELECT COUNT(*) FROM entry_content_links same WHERE same.content_object_id=l.content_object_id AND same.link_status='VERIFIED')>=2"
            )
        return " AND ".join(clauses), tuple(params)


def scoped_entry_ids(database, scope: AnalyzerScope, entry_type: str = "file") -> set[int]:
    where, params = scope.entry_query(entry_type)
    rows = database.fetch_all(
        f"""SELECT DISTINCT e.id FROM filesystem_entries e
            LEFT JOIN entry_content_links l ON l.entry_id=e.id
            LEFT JOIN file_signatures s ON s.entry_id=e.id
            LEFT JOIN classifications c ON c.entry_id=e.id WHERE {where}""",
        params,
    )
    return {int(row["id"]) for row in rows}
