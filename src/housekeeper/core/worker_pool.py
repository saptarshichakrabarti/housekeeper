from collections.abc import Callable, Iterable
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from typing import TypeVar

T = TypeVar("T")
U = TypeVar("U")


def bounded_map(
    fn: Callable[[T], U], items: Iterable[T], workers: int = 1, queue_size: int = 1000
) -> Iterable[U]:
    """Map with bounded in-flight futures, avoiding unbounded task queues.

    Yields results as they complete. ``set.pop()`` returned an arbitrary future, so the consumer
    blocked on whichever one the set happened to hand back even when others had already finished —
    head-of-line blocking dressed up as an ordering.
    """
    iterator = iter(items)
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        pending: set[Future[U]] = set()
        for _ in range(max(1, queue_size)):
            try:
                pending.add(pool.submit(fn, next(iterator)))
            except StopIteration:
                break
        while pending:
            completed, pending = wait(pending, return_when=FIRST_COMPLETED)
            for future in completed:
                yield future.result()
                try:
                    pending.add(pool.submit(fn, next(iterator)))
                except StopIteration:
                    pass
