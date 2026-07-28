"""Human-readable formatting helpers for reports (registered as Jinja filters)."""

from __future__ import annotations


def human_size(value) -> str:
    number = float(value or 0)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB", "PiB"):
        if number < 1024 or unit == "PiB":
            return f"{int(number)} B" if unit == "B" else f"{number:.1f} {unit}"
        number /= 1024
    return f"{number:.1f} PiB"


def percent(part, whole) -> str:
    whole = float(whole or 0)
    if not whole:
        return "0%"
    return f"{100 * float(part or 0) / whole:.1f}%"


#: What a redacted mount path is replaced by. Not an empty string: a reader has to be able to tell
#: "this path was shortened on purpose" from "this row has no path".
SOURCE_PLACEHOLDER = "<source>"


def redacts_paths(config) -> bool:
    return bool(config.section("reporting").get("redact_source_paths", False))


def display_path(absolute, relative, redact: bool) -> str:
    """The path a report should show for one entry.

    With redaction on, the source-relative path under a placeholder — which is the whole of what a
    reader needs and none of the account name or mount layout of the machine that produced the
    report. With it off, the absolute path, because an operator triaging their own drive wants
    something they can paste into a terminal.

    Falls back to whichever of the two exists, so a row with no relative path never silently
    becomes an empty cell.
    """
    if not redact:
        return str(absolute or relative or "")
    if relative:
        return f"{SOURCE_PLACEHOLDER}/{relative}"
    return SOURCE_PLACEHOLDER if absolute else ""


def display_source(source_root, redact: bool) -> str:
    """A source root's own label. The mount path *is* the sensitive part, so it goes entirely."""
    return SOURCE_PLACEHOLDER if redact else str(source_root or "")
