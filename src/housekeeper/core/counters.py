"""Machine-independent work counters.

Wall clock is noise in CI; units of work are not.  Hot paths increment a counter here so a test can
assert "an unchanged rescan reads zero source bytes" instead of "it finished in under five seconds".

Nothing is recorded outside a :func:`recording` block: :func:`count` returns immediately and the
SQLite trace callback is installed on entry and removed on exit, so instrumented code pays nothing
in production.
"""

from __future__ import annotations

import sqlite3
import sys
import threading
import time
import weakref
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager

_counts: Counter[str] = Counter()
_lock = threading.Lock()
_recording = False
# Open connections, tracked weakly so a recording block can attach a trace to connections that
# already existed without keeping a closed database's file handle alive.
_connections: weakref.WeakSet[Connection] = weakref.WeakSet()


class Connection(sqlite3.Connection):
    """The connection factory the database uses.

    Exists only so connections can be weakly referenced — ``sqlite3.Connection`` itself cannot be —
    and so each one registers itself for statement tracing on open.
    """

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        _connections.add(self)
        if _recording:
            self.set_trace_callback(_on_statement)


def count(name: str, amount: int = 1) -> None:
    """Add to a counter. A no-op — and cheap — when nothing is recording."""
    if not _recording:
        return
    with _lock:  # hashing runs in a thread pool; += on a dict entry is not atomic
        _counts[name] += amount


def is_recording() -> bool:
    """Whether a :func:`recording` block is active.

    Lets a caller skip the *measurement* — not just the ``count`` — when nothing will read it: a
    counter value that costs a syscall to produce (a WAL file size, a peak-RSS probe) should not be
    computed in production, where ``count`` would discard it anyway.
    """
    return _recording


def record_max(name: str, value: int) -> None:
    """Keep the largest ``value`` seen for ``name`` rather than a running sum.

    ``count`` accumulates, which is the right unit for "bytes read" but the wrong one for "peak WAL
    size": summing every stage's WAL bytes measures nothing. This keeps the maximum instead.
    """
    if not _recording:
        return
    with _lock:
        _counts[name] = max(_counts[name], value)


def _on_statement(sql: str) -> None:
    with _lock:
        _counts["sql_statements"] += 1
        if sql.startswith("COMMIT"):
            _counts["commits"] += 1


def _peak_rss_bytes() -> int:
    import resource

    peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    # Linux reports kilobytes, macOS/BSD bytes.
    return int(peak if sys.platform == "darwin" else peak * 1024)


def _trace(callback: object) -> None:
    for connection in list(_connections):
        try:
            connection.set_trace_callback(callback)  # type: ignore[arg-type]
        except sqlite3.ProgrammingError:
            pass  # already closed


@contextmanager
def recording() -> Iterator[Counter[str]]:
    """Collect counters for the duration of the block.

    The yielded ``Counter`` stays valid after the block ends (``peak_rss_bytes`` is filled in on
    exit), so callers can run a workload and assert on it afterwards.
    """
    global _recording, _counts
    previous, _counts = _counts, Counter()
    result = _counts
    _recording = True
    _trace(_on_statement)
    try:
        yield result
    finally:
        _recording = False
        _trace(None)
        result["peak_rss_bytes"] = _peak_rss_bytes()
        _counts = previous


@contextmanager
def stage(name: str) -> Iterator[None]:
    """Record one stage's duration in milliseconds as ``stage_ms:<name>``."""
    started = time.perf_counter()
    try:
        yield
    finally:
        count(f"stage_ms:{name}", int((time.perf_counter() - started) * 1000))
