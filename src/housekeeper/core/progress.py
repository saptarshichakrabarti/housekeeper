from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass
class Progress:
    processed: int = 0
    total: int | None = None
    errors: int = 0

    @property
    def fraction(self) -> float | None:
        return self.processed / self.total if self.total else None


def throughput(processed: int, elapsed_seconds: float) -> float:
    """Items per second; 0 when no time has meaningfully elapsed (never divide by zero)."""
    return processed / elapsed_seconds if elapsed_seconds > 0 else 0.0


def eta_seconds(processed: int, total: int | None, rate: float) -> float | None:
    """Seconds remaining, or ``None`` when the total or rate is unknown.

    An indeterminate operation (``total is None``) must never show an ETA.
    """
    if total is None or rate <= 0:
        return None
    return max(total - processed, 0) / rate


def seconds_since(sqlite_timestamp: str | None) -> float:
    """Seconds elapsed since a SQLite ``CURRENT_TIMESTAMP`` string (UTC), or 0 if absent."""
    if not sqlite_timestamp:
        return 0.0
    try:
        then = datetime.strptime(sqlite_timestamp, "%Y-%m-%d %H:%M:%S").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        return 0.0
    return max((datetime.now(timezone.utc) - then).total_seconds(), 0.0)


def format_duration(seconds: float) -> str:
    """Render seconds as ``mm:ss``, or ``h:mm:ss`` once it runs past an hour."""
    total = max(0, int(round(seconds)))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"
