import json
import time
from contextlib import contextmanager


@contextmanager
def stage_timer(metrics: dict, stage: str):
    start = time.perf_counter()
    try:
        yield
    finally:
        metrics.setdefault(stage, {})["seconds"] = time.perf_counter() - start


def write_profile(path, metrics: dict) -> None:
    path.write_text(json.dumps(metrics, indent=2, sort_keys=True), encoding="utf-8")
