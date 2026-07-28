"""Shared, parameterized analyser scope resolution.

The tool keeps a snapshot per scan, so "every row in ``filesystem_entries``" is not the drive — it
is the drive plus every earlier version of itself. An analyser that reads all of it will relate a
file to its own prior snapshot and conclude the current copy is a removable duplicate. That is
guardrail **G2**, and it has already been violated once.

Two things follow, and both are load-bearing:

* the current inventory is a **stored fact** (``source_roots.latest_complete_scan_run_id``) that
  resolves to literal run ids, so the predicate drives the composite index instead of being a
  subquery the planner evaluates against the whole table;
* :func:`resolve_scope` makes that the **default**. An analyser called without a scope now answers
  a question about the drive; asking about history is an explicit
  :meth:`AnalyserScope.all_history`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from ..path_utils import descendant_path_range


@dataclass(frozen=True)
class AnalyserScope:
    under: str | None = None
    source_id: int | None = None
    #: Which scan snapshots are in scope. Empty means every scan ever recorded — correct only for
    #: genuinely historical questions, which is why it is never the default at a call site.
    scan_run_ids: frozenset[int] = frozenset()
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

    @classmethod
    def current(cls, database, **overrides) -> AnalyserScope:
        """The current inventory: the latest COMPLETE scan of every source root."""
        return cls(scan_run_ids=current_inventory_runs(database), **overrides)

    @classmethod
    def for_run(cls, scan_run_id: int, **overrides) -> AnalyserScope:
        """One specific snapshot — what quickstart uses for the run it just produced."""
        return cls(scan_run_ids=frozenset({int(scan_run_id)}), **overrides)

    @classmethod
    def all_history(cls, **overrides) -> AnalyserScope:
        """Every snapshot ever recorded. Deliberately verbose: this is rarely what you want."""
        return cls(**overrides)

    def entry_query(self, entry_type: str = "file") -> tuple[str, tuple[object, ...]]:
        """The WHERE body shared by every scoped query, over aliases ``e``/``l``/``s``/``c``."""
        clauses = ["e.entry_type=?"]
        params: list[object] = [entry_type]
        if self.scan_run_ids:
            # Literal run ids, so the predicate drives (scan_run_id, ...) indexes. This was a
            # correlated subquery over scan_runs, which restricted nothing at plan time.
            runs = sorted(self.scan_run_ids)
            if len(runs) == 1:
                clauses.append("e.scan_run_id=?")
                params.append(runs[0])
            else:
                clauses.append("e.scan_run_id IN (" + ",".join("?" for _ in runs) + ")")
                params.extend(runs)
        if self.source_id is not None:
            clauses.append("e.source_root_id=?")
            params.append(self.source_id)
        if self.under:
            # An explicit range instead of LIKE 'prefix/%': the latter cannot use an index on the
            # path column, so scoping a run to one subtree cost a full table scan.
            low, high = descendant_path_range(self.under)
            clauses.append("e.absolute_path>=? AND e.absolute_path<?")
            params.extend((low, high))
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
            # One parameter, not one per id. This is the only facet whose size is set by the caller
            # rather than by the schema — `--content-object-id` accepts a list, and a scripted
            # caller passing more than SQLITE_MAX_VARIABLE_NUMBER (32,766 on current builds, 999 on
            # older ones) got an OperationalError instead of an answer. json_each turns the list
            # into a one-row-per-id table the planner materialises once.
            clauses.append("l.content_object_id IN (SELECT value FROM json_each(?))")
            params.append(json.dumps(sorted(self.content_object_ids)))
        if self.only_unique:
            clauses.append(
                "(SELECT COUNT(*) FROM entry_content_links same WHERE same.content_object_id=l.content_object_id AND same.link_status='VERIFIED')=1"
            )
        if self.only_duplicate_candidates:
            clauses.append(
                "(SELECT COUNT(*) FROM entry_content_links same WHERE same.content_object_id=l.content_object_id AND same.link_status='VERIFIED')>=2"
            )
        return " AND ".join(clauses), tuple(params)

    _JOINS = (
        "LEFT JOIN entry_content_links l ON l.entry_id=e.id "
        "LEFT JOIN file_signatures s ON s.entry_id=e.id "
        "LEFT JOIN classifications c ON c.entry_id=e.id"
    )

    def entry_id_sql(self, entry_type: str = "file") -> tuple[str, tuple[object, ...]]:
        """A subquery yielding the in-scope entry ids. **Embed it; never materialise it.**

        Callers used to fetch this into a Python set and filter rows they had already read —
        paying the full unscoped query, transfer and loop to reach a subset SQL could produce
        directly, and then binding the set as ``IN (?,?,…)``, which on the real inventory means
        1,226,510 parameters and an ``OperationalError``.
        """
        where, params = self.entry_query(entry_type)
        return f"SELECT e.id FROM filesystem_entries e {self._JOINS} WHERE {where}", params

    def content_object_id_sql(self, entry_type: str = "file") -> tuple[str, tuple[object, ...]]:
        """A subquery yielding the content objects reachable from the in-scope entries."""
        where, params = self.entry_query(entry_type)
        sql = (
            f"SELECT DISTINCT l.content_object_id FROM filesystem_entries e {self._JOINS} "
            f"WHERE l.content_object_id IS NOT NULL AND {where}"
        )
        return sql, params


def current_inventory_runs(database) -> frozenset[int]:
    """The latest COMPLETE scan run of each source root, as literal ids.

    Maintained by the scanner in the same transaction that marks a run COMPLETE, so reading it is
    a single indexed lookup over a table with one row per drive.
    """
    return frozenset(
        int(row["latest_complete_scan_run_id"])
        for row in database.fetch_all(
            "SELECT latest_complete_scan_run_id FROM source_roots "
            "WHERE latest_complete_scan_run_id IS NOT NULL"
        )
    )


def resolve_scope(database, scope: AnalyserScope | None) -> AnalyserScope:
    """The scope every current-state analyser should actually run with.

    ``None`` means the current inventory, not "everything ever scanned". An analyser that can be
    called without a scope will be, so the default has to be the safe answer rather than the
    dangerous one; :meth:`AnalyserScope.all_history` is how you ask for the other.
    """
    return AnalyserScope.current(database) if scope is None else scope
