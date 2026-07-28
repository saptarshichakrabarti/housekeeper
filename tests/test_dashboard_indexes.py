"""The new hot-path indexes are actually chosen by the planner.

Each case drops its index, confirms the query plan degrades to a table scan, recreates it, and
confirms the plan now seeks the index — the before/after EXPLAIN QUERY PLAN the task asks for.
"""

import pytest

# (index name, CREATE statement, probe query) — the probe mirrors the real dashboard query shape.
CASES = [
    (
        "idx_review_decisions_target",
        "CREATE INDEX idx_review_decisions_target ON review_decisions(target_type,target_id,current)",
        "SELECT decision FROM review_decisions WHERE target_type='ENTRY' AND target_id=1 AND current=1",
    ),
    (
        "idx_dupe_members_entry",
        "CREATE INDEX idx_dupe_members_entry ON exact_duplicate_members(entry_id)",
        "SELECT 1 FROM exact_duplicate_members dm WHERE dm.entry_id=1",
    ),
    # idx_entries_run_type_suffix_size is not here: on the empty `database` fixture the planner has
    # no statistics and picks between three viable indexes arbitrarily. It is asserted against the
    # 120k-row corpus in test_suffix_chart_index_is_run_leading below.
    (
        "idx_entries_suffix",
        "CREATE INDEX idx_entries_suffix ON filesystem_entries(suffix)",
        "SELECT id FROM filesystem_entries WHERE suffix='.pdf'",
    ),
]


def _plan(conn, sql: str) -> str:
    return " ".join(row["detail"] for row in conn.execute("EXPLAIN QUERY PLAN " + sql))


@pytest.mark.parametrize("index,create_sql,probe", CASES, ids=[case[0] for case in CASES])
def test_hot_query_uses_new_index(database, index, create_sql, probe):
    conn = database.connect()
    conn.execute(f"DROP INDEX IF EXISTS {index}")
    before = _plan(conn, probe)
    conn.execute(create_sql)
    after = _plan(conn, probe)
    assert index not in before, f"{index} used before it existed?\nplan: {before}"
    assert "SCAN" in before, f"expected a table scan without {index}\nplan: {before}"
    assert index in after, f"{index} not chosen after creation\nplan: {after}"


def test_search_does_not_need_a_standalone_path_index(metadata_corpus):
    """idx_entries_path is dropped as superseded; prove the search box did not regress.

    It was kept through the last review at 172 MB because the unscoped search planned through it.
    Now that the search reads ``current_entries``, ``UNIQUE(scan_run_id,relative_path)`` serves the
    filter *and* the ORDER BY. If someone rescopes the search back to the base table, this fails.
    """
    database, _run, _source = metadata_corpus
    conn = database.connect()
    probe = (
        "SELECT id,name,relative_path FROM current_entries "
        "WHERE relative_path LIKE 'q%' OR name LIKE 'q%' ORDER BY relative_path LIMIT 100"
    )
    plan = _plan(conn, probe)
    assert "idx_entries_path" not in {
        row["name"] for row in conn.execute("PRAGMA index_list('filesystem_entries')")
    }, "idx_entries_path is superseded and should have been dropped"
    assert "SCAN" not in plan, f"search degraded to a table scan\nplan: {plan}"
    assert "scan_run_id=?" in plan, f"search is not resolving through the run index\nplan: {plan}"


def test_suffix_chart_index_is_run_leading(metadata_corpus):
    """The file-types chart must seek one snapshot, not sweep every snapshot's files.

    Its index used to be ``(entry_type,suffix,size_bytes)``, sized for a chart that aggregated all
    of scan history. Once the chart read ``current_entries`` that index still answered it — as a
    covering scan over every snapshot, discarding all but the current one. One statement, cost
    growing with every rescan. Leading with ``scan_run_id`` is what makes it bounded, so this drops
    the index and proves the plan degrades without it.
    """
    from housekeeper.database import Database

    database, _run, _source = metadata_corpus
    conn = database.connect()
    index = "idx_entries_run_type_suffix_size"
    probe = Database._CHART_QUERIES["file_types"][1]
    try:
        conn.execute(f"DROP INDEX IF EXISTS {index}")
        conn.execute("ANALYZE")
        before = _plan(conn, probe)
        conn.execute(
            f"CREATE INDEX {index} ON filesystem_entries(scan_run_id,entry_type,suffix,size_bytes)"
        )
        conn.execute("ANALYZE")
        after = _plan(conn, probe)
    finally:  # session-scoped fixture: leave it exactly as the schema defines it
        conn.execute(
            f"CREATE INDEX IF NOT EXISTS {index} ON filesystem_entries(scan_run_id,entry_type,suffix,size_bytes)"
        )
        conn.execute("ANALYZE")
        conn.commit()
    assert index not in before, before
    assert index in after, f"{index} not chosen after creation\nplan: {after}"
    assert "scan_run_id=?" in after, f"chart is not seeking one snapshot\nplan: {after}"
