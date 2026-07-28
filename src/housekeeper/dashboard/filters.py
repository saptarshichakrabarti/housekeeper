from dataclasses import dataclass
from datetime import UTC, datetime
from html import escape
from math import isfinite

from markupsafe import Markup


def _as_int(value: object) -> int:
    if isinstance(value, (str, bytes, bytearray, int, float)):
        return int(value)
    return int(str(value))


def filesizeformat(value: object) -> Markup:
    """Render byte counts compactly while retaining the exact value for inspection."""

    try:
        exact_value = _as_int(value or 0)
    except (TypeError, ValueError):
        return Markup(escape(str(value or "")))
    size = float(exact_value)
    if not isfinite(size):
        return Markup(escape(str(value)))
    negative = size < 0
    size = abs(size)
    units = ("bytes", "KB", "MB", "GB", "TB", "PB")
    unit = units[0]
    scaled = size
    for unit in units:
        if scaled < 1000 or unit == units[-1]:
            break
        scaled /= 1000
    if unit == "bytes":
        label = f"{int(scaled):,} bytes"
    else:
        label = f"{scaled:.1f} {unit}"
    if negative:
        label = f"−{label}"
    exact = f"{exact_value:,} bytes"
    return Markup(
        f'<span class="number" data-bytes="{exact_value}" title="{escape(exact)}">'
        f"{escape(label)}</span>"
    )


def thousands(value: object) -> str:
    """Format integer-like counts with grouping separators."""

    if value is None or value == "":
        return ""
    try:
        return f"{_as_int(value):,}"
    except (TypeError, ValueError):
        return str(value)


def _as_datetime(value: object) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, (int, float)):
        parsed = datetime.fromtimestamp(float(value), tz=UTC)
    else:
        text = str(value).strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)


def relativetime(value: object, now: datetime | None = None) -> Markup:
    """Render timestamps as a concise relative age with the exact UTC time as a tooltip."""

    parsed = _as_datetime(value)
    if parsed is None:
        return Markup(escape(str(value or "")))
    reference = now or datetime.now(UTC)
    if reference.tzinfo is None:
        reference = reference.replace(tzinfo=UTC)
    seconds = int((reference.astimezone(UTC) - parsed).total_seconds())
    future = seconds < 0
    age = abs(seconds)
    if age < 60:
        amount, unit = age, "s"
    elif age < 3600:
        amount, unit = age // 60, "min"
    elif age < 86400:
        amount, unit = age // 3600, "h"
    elif age < 2_592_000:
        amount, unit = age // 86400, "d"
    elif age < 31_536_000:
        amount, unit = age // 2_592_000, "mo"
    else:
        amount, unit = age // 31_536_000, "y"
    label = f"in {amount} {unit}" if future else f"{amount} {unit} ago"
    exact = parsed.isoformat().replace("+00:00", "Z")
    return Markup(
        f'<time class="number" datetime="{escape(exact)}" title="{escape(exact)}">'
        f"{escape(label)}</time>"
    )


@dataclass(frozen=True)
class ReviewFilter:
    classification: str | None = None
    source_root_id: int | None = None
    minimum_confidence: float | None = None
    maximum_confidence: float | None = None
    suffix: str | None = None
    minimum_size: int | None = None
    maximum_size: int | None = None
    decision: str | None = None
    reason_code: str | None = None
    minimum_age_timestamp: float | None = None
    maximum_age_timestamp: float | None = None
    duplicate_only: bool = False
    project_only: bool = False
    stale: bool | None = None
    protected: bool | None = None
    top_level_directory: str | None = None

    def where_clause(self) -> tuple[str, tuple[object, ...]]:
        clauses = ["e.entry_type='file'"]
        params: list[object] = []
        if self.classification:
            clauses.append("c.classification=?")
            params.append(self.classification)
        if self.minimum_confidence is not None:
            clauses.append("c.confidence>=?")
            params.append(self.minimum_confidence)
        if self.maximum_confidence is not None:
            clauses.append("c.confidence<=?")
            params.append(self.maximum_confidence)
        if self.suffix:
            clauses.append("e.suffix=?")
            params.append(self.suffix.lower())
        if self.minimum_size is not None:
            clauses.append("e.size_bytes>=?")
            params.append(self.minimum_size)
        if self.maximum_size is not None:
            clauses.append("e.size_bytes<=?")
            params.append(self.maximum_size)
        if self.source_root_id is not None:
            clauses.append("e.source_root_id=?")
            params.append(self.source_root_id)
        if self.decision:
            clauses.append(
                "EXISTS(SELECT 1 FROM review_decisions d WHERE d.target_type='ENTRY' AND d.target_id=e.id AND d.current=1 AND d.decision=?)"
            )
            params.append(self.decision)
        if self.reason_code:
            clauses.append("c.reason_codes_json LIKE ?")
            params.append(f'%"{self.reason_code}"%')
        if self.minimum_age_timestamp is not None:
            clauses.append("e.modified_at>=?")
            params.append(self.minimum_age_timestamp)
        if self.maximum_age_timestamp is not None:
            clauses.append("e.modified_at<=?")
            params.append(self.maximum_age_timestamp)
        if self.duplicate_only:
            clauses.append(
                "EXISTS(SELECT 1 FROM current_exact_duplicate_members dm WHERE dm.entry_id=e.id)"
            )
        if self.project_only:
            clauses.append("EXISTS(SELECT 1 FROM current_projects p WHERE p.root_entry_id=e.id)")
        if self.stale is not None:
            clauses.append(
                "EXISTS(SELECT 1 FROM review_decisions d WHERE d.target_type='ENTRY' AND d.target_id=e.id AND d.current=1 AND d.stale=?)"
            )
            params.append(int(self.stale))
        if self.protected is not None:
            clauses.append(
                "c.classification=?"
                if self.protected
                else "(c.classification IS NULL OR c.classification<>'PROTECTED')"
            )
            if self.protected:
                params.append("PROTECTED")
        if self.top_level_directory:
            clauses.append("(e.relative_path=? OR e.relative_path LIKE ?)")
            params.extend((self.top_level_directory, f"{self.top_level_directory}/%"))
        return " AND ".join(clauses), tuple(params)
