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
    (
        "idx_entries_type_suffix_size",
        "CREATE INDEX idx_entries_type_suffix_size ON filesystem_entries(entry_type,suffix,size_bytes)",
        "SELECT COALESCE(suffix,'(none)') s,COUNT(*),SUM(size_bytes) FROM filesystem_entries WHERE entry_type='file' GROUP BY suffix",
    ),
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
