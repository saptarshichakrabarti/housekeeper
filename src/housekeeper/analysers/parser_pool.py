"""Persistent out-of-process parser pool for untrusted files.

Workers persist (~0.3 ms overhead vs 8–11 ms per-process spawn). Prefer ``forkserver`` so children
do not inherit locks from a threaded parent (dashboard). Tasks cross the boundary as
``(spec_name, path)`` — no pickled closures.

A timed-out parse destroys its worker; tasks that shared that pool get one retry.
"""

from __future__ import annotations

import multiprocessing
import os
import sys
import threading
from typing import Any, Self


def _forkserver_is_usable() -> bool:
    """Whether ``forkserver`` can actually start a worker here.

    ``forkserver`` inherits ``spawn``'s semantics: the child re-imports ``__main__``. If
    ``__main__`` names a file that does not exist — a piped script, an embedding host, some
    interactive shells — every worker dies during import, forever, and the pool never comes up.
    Checked once, up front, rather than discovered as a hang.
    """
    if "forkserver" not in multiprocessing.get_all_start_methods():
        return False
    main = sys.modules.get("__main__")
    path = getattr(main, "__file__", None)
    return path is None or os.path.isfile(path)


def _start_method() -> str | None:
    """Best available process isolation, safest first.

    Prefer ``forkserver`` (clean single-threaded template). Allow plain ``fork`` only from a
    single-threaded parent — forking under the dashboard thread pool fails on macOS during ObjC
    init, and pool retries hide it as a slow noisy run. ``None`` falls back to bounded in-process
    parsing.
    """
    if _forkserver_is_usable():
        return "forkserver"
    if "fork" in multiprocessing.get_all_start_methods() and threading.active_count() == 1:
        return "fork"
    return None

_worker_config: Any = None
_worker_memory_limit_mb: int | None = None


def _initialise_worker(config_data: dict, workspace: str, memory_limit_mb: int | None) -> None:
    """Rebuild the config in the worker and cap its address space, once per worker."""
    global _worker_config, _worker_memory_limit_mb
    from pathlib import Path

    from ..config import AppConfig

    _worker_config = AppConfig(config_data, Path(workspace))
    _worker_memory_limit_mb = memory_limit_mb
    if memory_limit_mb:
        try:
            import resource

            limit = memory_limit_mb * 1024 * 1024
            resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
        except (ImportError, OSError, ValueError):
            pass


def _parse(task: tuple[str, str]) -> dict[str, Any]:
    """Run one analyser over one path. Data in, data out — nothing else crosses the boundary."""
    spec_name, path = task
    from pathlib import Path

    from .registry import REGISTRY

    try:
        spec = next(candidate for candidate in REGISTRY if candidate.name == spec_name)
        return spec.runner(Path(path), _worker_config)
    except BaseException as exc:  # noqa: BLE001 - reported as an artifact, never raised at the caller
        return {"analysis_status": "ERROR", "analysis_error": f"{type(exc).__name__}: {exc}"}


def _timeout_result(timeout_seconds: int) -> dict[str, Any]:
    return {
        "analysis_status": "ERROR",
        "analysis_error": f"parser timeout after {timeout_seconds}s",
    }


class ParserPool:
    """Persistent parser workers, or a synchronous fallback where forkserver is unavailable."""

    def __init__(self, config, workers: int, memory_limit_mb: int | None = None) -> None:
        self.workers = max(1, int(workers))
        self._config = config
        self._memory_limit_mb = memory_limit_mb
        self._pool: Any = None
        # `run` is called from several submitter threads at once, so the pool handle itself needs
        # guarding: a timeout on one file destroys the pool that the others are mid-call on.
        # `multiprocessing.Pool` is itself thread-safe; this protects the create/destroy edges.
        self._lock = threading.Lock()
        method = _start_method()
        self._context = multiprocessing.get_context(method) if method else None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _ensure_pool(self) -> Any:
        with self._lock:
            if self._pool is None and self._context is not None:
                # Counted here, where processes are actually created — the counter measures process
                # starts, and with a pool that is per worker rather than per parse.
                from ..core import counters

                counters.count("parser_processes_started", self.workers)
                self._pool = self._context.Pool(
                    processes=self.workers,
                    initializer=_initialise_worker,
                    initargs=(
                        self._config.data,
                        str(self._config.workspace),
                        self._memory_limit_mb,
                    ),
                    # Recycled periodically so a parser that leaks memory cannot accumulate across
                    # a whole corpus, and so an address-space cap is re-applied to a fresh process.
                    maxtasksperchild=256,
                )
            return self._pool

    def _restart(self, doomed: Any) -> None:
        """Destroy ``doomed`` — and with it any wedged worker — if it is still the live pool.

        Takes the pool it means to kill, so two submitter threads timing out at once destroy one
        pool between them rather than one destroying the replacement the other just built.
        """
        with self._lock:
            if self._pool is not doomed or doomed is None:
                return
            self._pool = None
        doomed.terminate()
        doomed.join()

    def close(self) -> None:
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.close()
            pool.join()

    def run(self, spec_name: str, path: str, timeout_seconds: int) -> dict[str, Any]:
        """Parse one file, returning its result or an error artifact. Never raises.

        Safe to call from several threads at once: that is how the artifact stage keeps every
        worker busy, since a pool of N is only worth N if N parses are in flight.
        """
        if timeout_seconds < 1:
            raise ValueError("parser timeout must be positive")
        pool = self._ensure_pool()
        if pool is None:
            return _run_without_pool(spec_name, path, timeout_seconds, self._config)
        for attempt in (1, 2):
            try:
                return pool.apply_async(_parse, ((spec_name, path),)).get(timeout_seconds)
            except multiprocessing.TimeoutError:
                # The worker is wedged on this file: destroy the pool so it cannot hold a slot.
                self._restart(pool)
                return _timeout_result(timeout_seconds)
            except (BrokenPipeError, EOFError, OSError, ValueError):
                # A worker died — killed by the memory cap, or terminated with a pool that another
                # task poisoned. Rebuild once and try again before calling it an error. ValueError
                # is `Pool not running`, which is what a concurrent restart looks like from here.
                self._restart(pool)
                if attempt == 2:
                    return {
                        "analysis_status": "ERROR",
                        "analysis_error": "parser worker died",
                    }
                pool = self._ensure_pool()
                if pool is None:
                    return _run_without_pool(spec_name, path, timeout_seconds, self._config)
        return {"analysis_status": "ERROR", "analysis_error": "parser worker died"}


def _run_without_pool(spec_name: str, path: str, timeout_seconds: int, config) -> dict[str, Any]:
    """Bounded in-process fallback for platforms with no forkserver (Windows).

    Same contract, weaker isolation — which is why it is the fallback and not the default.
    """
    from concurrent.futures import ThreadPoolExecutor
    from concurrent.futures import TimeoutError as FutureTimeout
    from pathlib import Path

    from ..core import counters
    from .registry import REGISTRY

    spec = next(candidate for candidate in REGISTRY if candidate.name == spec_name)
    counters.count("parser_processes_started")
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        return pool.submit(spec.runner, Path(path), config).result(timeout=timeout_seconds)
    except FutureTimeout:
        return _timeout_result(timeout_seconds)
    except BaseException as exc:  # noqa: BLE001 - an artifact, never a recommendation
        return {"analysis_status": "ERROR", "analysis_error": f"{type(exc).__name__}: {exc}"}
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def worker_count(config) -> int:
    """Honour ``performance.parser_workers`` — which the parser loop never actually read."""
    from ..config import performance_profile

    return max(1, int(performance_profile(config)["parser_workers"]))
