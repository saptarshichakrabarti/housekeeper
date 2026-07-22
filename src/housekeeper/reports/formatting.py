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
