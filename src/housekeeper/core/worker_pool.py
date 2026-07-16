from concurrent.futures import Future, ThreadPoolExecutor, TimeoutError
import multiprocessing
from queue import Empty
from typing import Any
from typing import Callable, Iterable, TypeVar

T = TypeVar("T")
U = TypeVar("U")


def bounded_map(
    fn: Callable[[T], U], items: Iterable[T], workers: int = 1, queue_size: int = 1000
) -> Iterable[U]:
    """Map with bounded in-flight futures, avoiding unbounded task queues."""
    iterator = iter(items)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        pending: set[Future[U]] = set()
        for _ in range(max(1, queue_size)):
            try:
                pending.add(pool.submit(fn, next(iterator)))
            except StopIteration:
                break
        while pending:
            future = pending.pop()
            yield future.result()
            try:
                pending.add(pool.submit(fn, next(iterator)))
            except StopIteration:
                pass


def run_parser_isolated(
    fn: Callable[[], dict[str, Any]], timeout_seconds: int, memory_limit_mb: int | None = None
) -> dict[str, Any]:
    """Run untrusted parser work out of process where fork is available.

    Windows' spawn model cannot safely serialize arbitrary optional-parser callables, so it
    deliberately uses the same bounded synchronous fallback rather than weakening results.
    """
    if timeout_seconds < 1:
        raise ValueError("parser timeout must be positive")
    if "fork" not in multiprocessing.get_all_start_methods():
        # Spawn cannot serialize optional-parser closures reliably. A bounded thread is
        # the portable fallback: it returns on timeout and has no unbounded queue.
        pool = ThreadPoolExecutor(max_workers=1)
        future = pool.submit(fn)
        try:
            return future.result(timeout=timeout_seconds)
        except TimeoutError:
            future.cancel()
            return {
                "analysis_status": "ERROR",
                "analysis_error": f"parser timeout after {timeout_seconds}s",
            }
        except BaseException as exc:
            return {"analysis_status": "ERROR", "analysis_error": f"{type(exc).__name__}: {exc}"}
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    context = multiprocessing.get_context("fork")
    queue: multiprocessing.Queue[Any] = context.Queue(maxsize=1)

    def target() -> None:
        try:
            if memory_limit_mb:
                try:
                    import resource

                    limit = memory_limit_mb * 1024 * 1024
                    resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
                except (ImportError, OSError, ValueError):
                    pass
            queue.put((True, fn()))
        except BaseException as exc:
            queue.put((False, f"{type(exc).__name__}: {exc}"))

    process = context.Process(target=target, daemon=True)
    process.start()
    process.join(timeout_seconds)
    if process.is_alive():
        process.terminate()
        process.join()
        return {
            "analysis_status": "ERROR",
            "analysis_error": f"parser timeout after {timeout_seconds}s",
        }
    try:
        ok, value = queue.get(timeout=0.2)
    except Empty:
        return {"analysis_status": "ERROR", "analysis_error": "parser process returned no result"}
    return value if ok else {"analysis_status": "ERROR", "analysis_error": str(value)}
