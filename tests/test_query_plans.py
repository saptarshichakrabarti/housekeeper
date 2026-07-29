"""Query-plan regression tests for the hot paths.

Two of the twenty quickstart stages could not complete on a real 1.3M-entry inventory because a
point lookup was planned against an index that only narrowed it to "everything in this scan run".
Nothing in the suite caught it: on a toy database SQLite scans a few hundred rows and the plan
never matters.

So these tests run ``EXPLAIN QUERY PLAN`` against a corpus with real statistics (the
``metadata_corpus`` fixture) and assert the *shape* of the plan. Asserting only "not a SCAN" is too
weak — ``SEARCH … USING INDEX idx_entries_run (scan_run_id=?)`` is a SEARCH and still visits every
row of the run — so each case also names the predicates the plan must resolve through an index.
Where a full sweep is the correct plan, the case constrains the inner tables instead.
"""

from __future__ import annotations

import pytest


class Case:
    """One hot-path query and what its plan must and must not contain."""

    def __init__(self, name, sql, params=(), requires=(), forbids=()):
        self.name, self.sql, self.params = name, sql, params
        self.requires, self.forbids = requires, forbids


HOT_PATH_QUERIES = [
    Case(
        # The 58-hour stage: narrowing to the scan run alone leaves 600k rows per directory.
        "child_by_parent",
        "SELECT name FROM filesystem_entries WHERE scan_run_id=? AND parent_entry_id=?",
        (1, 1),
        requires=("parent_entry_id=?",),
        forbids=("SCAN filesystem_entries",),
    ),
    Case(
        # Phase 1.2's range rewrite; the index may be idx_entries_run_relative or the UNIQUE
        # autoindex that supersedes it, so assert the predicate rather than the index name.
        "descendant_prefix",
        """SELECT e.size_bytes FROM filesystem_entries e
           WHERE e.scan_run_id=? AND e.entry_type='file'
           AND e.relative_path>=? AND e.relative_path<?""",
        (1, "00001/", "00001/\U0010ffff"),
        requires=("scan_run_id=?", "relative_path>"),
        forbids=("SCAN e", "SCAN filesystem_entries"),
    ),
    Case(
        "rename_by_size",
        """SELECT e.id,e.relative_path,s.quick_hash FROM filesystem_entries e
           JOIN file_signatures s ON s.entry_id=e.id
           WHERE e.scan_run_id=? AND e.size_bytes=? AND s.quick_hash IS NOT NULL""",
        (1, 1024),
        requires=("scan_run_id=? AND size_bytes=?",),
        forbids=("SCAN e", "SCAN s", "SCAN filesystem_entries"),
    ),
    Case(
        "reappearance_after_missing",
        """SELECT old.id,old.scan_run_id FROM filesystem_entries old
           JOIN scan_entry_changes change ON change.entry_id=old.id
           WHERE old.source_root_id=? AND old.relative_path=?
           AND change.change_status='MISSING' ORDER BY change.id DESC LIMIT 1""",
        (1, "00001/item-0000500.dat"),
        requires=("source_root_id=? AND relative_path=?",),
        forbids=("SCAN change", "SCAN scan_entry_changes", "SCAN old"),
    ),
    Case(
        "size_funnel",
        """SELECT e.id FROM filesystem_entries e
           LEFT JOIN entry_content_links l ON l.entry_id=e.id AND l.link_status='VERIFIED'
           WHERE e.entry_type='file' AND e.size_bytes=? AND l.entry_id IS NULL ORDER BY e.id""",
        (1024,),
        requires=("size_bytes=?",),
        forbids=("SCAN e", "SCAN l", "SCAN entry_content_links"),
    ),
    Case(
        # Visiting every unlinked file is correct here; the anti-join side must not be a scan.
        "unlinked_anti_join",
        """SELECT e.id FROM filesystem_entries e
           LEFT JOIN entry_content_links l ON l.entry_id=e.id
           WHERE e.entry_type='file' AND l.entry_id IS NULL ORDER BY e.id""",
        forbids=("SCAN l", "SCAN entry_content_links"),
    ),
    # The per-spec content sweep is deliberately *not* re-typed here. Re-typing it was how its
    # scope predicate went missing without any test noticing: the stand-in stayed correct while the
    # real query silently regressed. It is asserted against the SQL the stage actually generates,
    # in test_generated_content_work_plan_is_scoped_and_indexed below.
    Case(
        # The classification sweep must visit every file; what must not happen is a per-row scan
        # of the duplicate-member or content-link tables.
        "classification_sweep",
        """SELECT e.id,
             (SELECT COUNT(*) FROM exact_duplicate_members m2 WHERE m2.group_id=m.group_id) AS group_size,
             EXISTS(SELECT 1 FROM entry_content_links l WHERE l.entry_id=e.id) AS linked
           FROM filesystem_entries e
           LEFT JOIN exact_duplicate_members m ON m.entry_id=e.id
           WHERE e.entry_type='file'""",
        forbids=("SCAN m", "SCAN m2", "SCAN l", "SCAN exact_duplicate_members"),
    ),
    Case(
        # Cross-drive coverage: one indexed anti-join per source. Visiting every file of the source
        # is the point; what must not happen is a scan of the content-link table per file.
        "coverage_membership",
        """SELECT CASE WHEN l.entry_id IS NULL THEN 'unknown'
                  WHEN EXISTS(SELECT 1 FROM entry_content_links l2
                              JOIN current_entries o ON o.id=l2.entry_id
                              WHERE l2.content_object_id=l.content_object_id
                                AND o.source_root_id<>e.source_root_id)
                  THEN 'covered' ELSE 'unique' END state, COUNT(*) n
           FROM current_entries e
           LEFT JOIN entry_content_links l ON l.entry_id=e.id AND l.link_status='VERIFIED'
           WHERE e.source_root_id=? AND e.entry_type='file' GROUP BY state""",
        (1,),
        forbids=("SCAN l", "SCAN l2", "SCAN entry_content_links"),
    ),
]


def _plan(database, sql: str, params: tuple) -> str:
    rows = database.fetch_all(f"EXPLAIN QUERY PLAN {sql}", params)
    return "\n".join(str(row["detail"]) for row in rows)


@pytest.mark.parametrize("case", HOT_PATH_QUERIES, ids=[case.name for case in HOT_PATH_QUERIES])
def test_hot_path_query_plan(metadata_corpus, case):
    database, _run_id, _source_id = metadata_corpus
    # The plan depends on the schema and the statistics, not on the bound values.
    plan = _plan(database, case.sql, case.params)
    for fragment in case.forbids:
        assert fragment not in plan, f"{case.name}: plan contains {fragment!r}\n{plan}"
    for fragment in case.requires:
        assert fragment in plan, f"{case.name}: plan never resolves {fragment!r}\n{plan}"


def test_corpus_is_large_enough_for_real_statistics(metadata_corpus):
    """Plans differ between a toy database and one with statistics; assert the premise holds."""
    database, _run_id, _source_id = metadata_corpus
    row = database.fetch_one("SELECT COUNT(*) AS n FROM filesystem_entries")
    assert int(row["n"]) >= 100_000
    assert database.fetch_one("SELECT 1 FROM sqlite_master WHERE name='sqlite_stat1'") is not None


def test_current_inventory_scope_drives_the_index(metadata_corpus):
    """3.1: the current inventory is a stored fact, so the scope binds literal run ids.

    It used to be ``scan_run_id IN (SELECT MAX(id) FROM scan_runs … GROUP BY …)``, which the
    planner cannot use to restrict the driving table at all — the scope narrowed nothing.
    """
    from housekeeper.analysers.scope import AnalyserScope

    database, run_id, _source_id = metadata_corpus
    scope = AnalyserScope(scan_run_ids=frozenset({run_id}))
    where, params = scope.entry_query()
    plan = _plan(database, f"SELECT e.id FROM filesystem_entries e WHERE {where}", params)
    assert "scan_run_id=?" in plan, plan
    assert "SCAN e" not in plan, plan


def test_scope_subquery_is_a_seek_not_a_scan(metadata_corpus):
    """3.3: `scoped_entry_ids` fetched every id into Python; the replacement stays in SQL."""
    from housekeeper.analysers.scope import AnalyserScope

    database, run_id, _source_id = metadata_corpus
    entry_sql, params = AnalyserScope(scan_run_ids=frozenset({run_id})).entry_id_sql()
    plan = _plan(
        database,
        f"SELECT COUNT(*) FROM filesystem_entries outer_e WHERE outer_e.id IN ({entry_sql})",
        params,
    )
    assert "scan_run_id=?" in plan, plan


def test_migration_v8_statements_are_indexed(tmp_path):
    """The upgrade path needs the same discipline as the hot paths — it did not have it.

    Both v7->v8 statements were written against a toy database. On the real inventory (181,071
    duplicate groups, 271,936 content objects) the backfill looked up by ``(full_hash, size_bytes)``
    — not a usable prefix of ``UNIQUE(hash_algorithm, full_hash, size_bytes)`` — and the defensive
    pass was an ``id NOT IN (SELECT MIN(id) ... GROUP BY ...)`` that did not complete in eleven
    minutes. Two throwaway indexes took the migration to 1.3 s. This asserts the planner actually
    uses them, which a timing test could only imply.
    """
    from housekeeper.database import Database

    database = Database(tmp_path / "plans.sqlite")
    database.initialize()
    connection = database.connect()
    for statement in Database._V8_HELPER_INDEXES:
        connection.execute(statement)
    # Predicates, not index names: on a fresh database the unique partial index already exists and
    # the planner may prefer it to the throwaway one. What matters is that neither inner lookup is
    # a scan repeated per row.
    backfill = _plan(database, Database._V8_BACKFILL, ())
    assert "full_hash=? AND size_bytes=?" in backfill, backfill
    assert "SCAN co" not in backfill, backfill
    dedupe = _plan(database, Database._V8_DEDUPE, ())
    assert "content_object_id=?" in dedupe, dedupe
    assert "SCAN other" not in dedupe, dedupe


def test_generated_content_work_plan_is_scoped_and_indexed(metadata_corpus):
    """The *actual* plan the content stage runs, not a hand-written stand-in.

    Every other case in this file re-types a query by hand, so a plan could regress in the real
    code while the test kept passing. This one asks ``_work_plan`` for the SQL it will genuinely
    execute and checks two things the stage got wrong at once: that the current-inventory predicate
    is present at all, and that it resolves through an index rather than filtering after the fact.
    """
    from housekeeper.analysers.registry import REGISTRY, _work_plan
    from housekeeper.analysers.scope import AnalyserScope

    database, _run_id, _source = metadata_corpus
    spec = next(item for item in REGISTRY if item.name == "documents")
    sql, params = _work_plan(spec, "fp", False, AnalyserScope.current(database))
    plan = _plan(database, sql, params)
    assert "scan_run_id=?" in plan, f"work plan is not scoped to a snapshot\nplan: {plan}"
    assert "SCAN filesystem_entries" not in plan, plan
    assert "SCAN e" not in plan, plan


def test_work_plan_representatives_never_come_from_a_historical_snapshot(metadata_corpus):
    """The silent-skip defect, at the point of failure.

    The plan used to select non-aggregated entry columns under ``GROUP BY co.id``, so SQLite was
    free to return a row from *any* snapshot linked to that content object — and the caller then
    dropped objects whose row was not from the requested run. Both halves are gone: membership is
    an ``EXISTS`` over the scope, and the representatives are the in-scope entries by construction.
    """
    import json

    from housekeeper.analysers.registry import REGISTRY, _work_plan
    from housekeeper.analysers.scope import AnalyserScope, current_inventory_runs

    database, run_id, _source = metadata_corpus
    spec = next(item for item in REGISTRY if item.name == "documents")
    sql, params = _work_plan(
        spec, "fp", False, AnalyserScope.current(database), pending_only=False
    )
    current_runs = current_inventory_runs(database)
    assert current_runs == {run_id}

    rows = database.fetch_all(sql + " LIMIT 200", params)
    assert rows, "the corpus should contain analysable documents"
    entry_ids = [
        entry_id for row in rows for entry_id, _path in json.loads(row["representatives_json"])
    ]
    placeholders = ",".join("?" for _ in entry_ids)
    stray = database.fetch_all(
        f"SELECT DISTINCT scan_run_id FROM filesystem_entries WHERE id IN ({placeholders}) "
        "AND scan_run_id<>?",
        (*entry_ids, run_id),
    )
    assert not stray, f"work plan returned representatives from snapshots {[r[0] for r in stray]}"
