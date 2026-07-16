"""Bounded single-writer queue for bulk SQLite mutations."""

from __future__ import annotations

from queue import Queue
from threading import Event, Thread
from typing import Any


class DatabaseWriter:
    def __init__(self, database: Any, batch_size: int = 1_000, queue_size: int = 10_000):
        self.database = database
        self.batch_size = max(1, batch_size)
        self.queue: Queue[tuple[str, tuple[Any, ...]] | None] = Queue(maxsize=max(1, queue_size))
        self.finished = Event()
        self.error: BaseException | None = None
        self.thread = Thread(target=self._run, name="housekeeper-db-writer", daemon=True)

    def __enter__(self) -> "DatabaseWriter":
        self.thread.start()
        return self

    def submit(self, sql: str, params: tuple[Any, ...]) -> None:
        if self.error:
            raise RuntimeError("database writer failed") from self.error
        self.queue.put((sql, params))

    def _run(self) -> None:
        pending: dict[str, list[tuple[Any, ...]]] = {}
        try:
            while True:
                item = self.queue.get()
                if item is None:
                    break
                sql, params = item
                rows = pending.setdefault(sql, [])
                rows.append(params)
                if len(rows) >= self.batch_size:
                    self.database.executemany(sql, rows)
                    pending.pop(sql, None)
            for sql, rows in pending.items():
                self.database.executemany(sql, rows)
        except BaseException as exc:
            self.error = exc
        finally:
            self.finished.set()

    def close(self) -> None:
        self.queue.put(None)
        self.thread.join()
        if self.error:
            raise RuntimeError("database writer failed") from self.error

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self.close()
