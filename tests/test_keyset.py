"""Keyset paging must return exactly what a plain cursor would — every row, once, in order.

The paged reader closes and reopens its cursor between pages so a long stage never pins the WAL; the
risk that buys is a skipped or duplicated row at a page boundary, or a mishandled NULL in the
composite key. These tests hold the paged stream to a plain query's result for a range of page sizes
and for rows whose device/inode are NULL.
"""

from __future__ import annotations

from housekeeper.core.identity import _identity_boundary, _IDENTITY_KEY_EXPRS
from housekeeper.database import Database


def _seed(database, rows):
    run = database.create_scan_run("/x", "fp", "test")
    for rel, dev, ino in rows:
        database.connect().execute(
            """INSERT INTO filesystem_entries(scan_run_id,source_root,absolute_path,relative_path,name,
               entry_type,size_bytes,device_id,inode_or_file_id,scan_status)
               VALUES(?,?,?,?,?,'file',1,?,?,'OK')""",
            (run, "/x", f"/x/{rel}", rel, rel, dev, ino),
        )
    database.connect().commit()
    return run


def _plain(database, run):
    return [
        r["id"]
        for r in database.fetch_all(
            "SELECT e.id FROM filesystem_entries e WHERE e.scan_run_id=? "
            "ORDER BY COALESCE(e.device_id,-1),COALESCE(e.inode_or_file_id,-1),e.id",
            (run,),
        )
    ]


def _keyset(database, run, batch_size):
    sql = "SELECT e.id,e.device_id,e.inode_or_file_id FROM filesystem_entries e WHERE e.scan_run_id=?{keyset}"
    return [
        r["id"]
        for r in database.reader().iter_keyset(
            sql, (run,), key_exprs=_IDENTITY_KEY_EXPRS, key_of=_identity_boundary, batch_size=batch_size
        )
    ]


def test_keyset_matches_a_plain_ordered_query(database):
    # Hard-link mates (shared device+inode), distinct files, and rows with NULL device/inode.
    rows = []
    for i in range(50):
        rows.append((f"f{i:03d}", 64, 1000 + i))
    for i in range(5):  # five paths sharing one inode — must stay adjacent
        rows.append((f"link{i}", 64, 9999))
    for i in range(7):  # NULL device/inode — must not be skipped
        rows.append((f"null{i}", None, None))
    run = _seed(database, rows)

    expected = _plain(database, run)
    assert len(expected) == 62
    for batch_size in (1, 2, 7, 61, 62, 63, 1000):
        got = _keyset(database, run, batch_size)
        assert got == expected, f"batch_size={batch_size}: keyset diverged from the plain query"


def test_keyset_keeps_inode_mates_adjacent(database):
    """The whole point of the ordering is hard-link adjacency; paging must not break it."""
    rows = [("a", 64, 1), ("shared0", 64, 500), ("b", 64, 2), ("shared1", 64, 500), ("c", 64, 3)]
    run = _seed(database, rows)
    ids = _keyset(database, run, batch_size=2)
    names = {
        r["id"]: r["name"]
        for r in database.fetch_all("SELECT id,name FROM filesystem_entries WHERE scan_run_id=?", (run,))
    }
    ordered = [names[i] for i in ids]
    # The two inode-500 rows must be consecutive despite a tiny page size.
    i0, i1 = ordered.index("shared0"), ordered.index("shared1")
    assert abs(i0 - i1) == 1


def test_keyset_rejects_a_nonpositive_batch(database):
    import pytest

    with pytest.raises(ValueError):
        list(
            database.reader().iter_keyset(
                "SELECT id,device_id,inode_or_file_id FROM filesystem_entries WHERE 1=1{keyset}",
                (),
                key_exprs=_IDENTITY_KEY_EXPRS,
                key_of=_identity_boundary,
                batch_size=0,
            )
        )
