"""The dashboard's reads run on an independent, read-only, per-thread connection pool.

WAL already allows many readers alongside one writer; these tests pin the contract that the pool
exists, is genuinely read-only, is thread-local, and observes the writer's committed data.
"""

import sqlite3
import threading

import pytest


def test_reader_is_pooled_and_not_the_writer(database):
    reader = database.reader()
    assert reader.fetch_one("SELECT 1 n")["n"] == 1
    # Same thread reuses one pooled connection; it is never the writer connection.
    assert database._read_conn() is database._read_conn()
    assert database._read_conn() is not database.connect()


def test_read_connection_rejects_writes(database):
    with database.read_connection() as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("CREATE TABLE nope(x)")


def test_pooled_reader_rejects_writes(database):
    with pytest.raises(sqlite3.OperationalError):
        database._read_conn().execute("CREATE TABLE nope(x)")


def test_reader_sees_committed_writer_data(database):
    database.connect().execute(
        "INSERT INTO source_roots(display_name,source_fingerprint,last_mount_path) VALUES('d','fp','/m')"
    )
    database.connect().commit()
    assert database.reader().fetch_one("SELECT COUNT(*) n FROM source_roots")["n"] == 1


def test_each_thread_gets_its_own_read_connection(database):
    seen: dict[str, int] = {}

    def grab(name: str) -> None:
        seen[name] = id(database._read_conn())

    worker = threading.Thread(target=grab, args=("worker",))
    worker.start()
    worker.join()
    grab("main")
    assert seen["worker"] != seen["main"]
