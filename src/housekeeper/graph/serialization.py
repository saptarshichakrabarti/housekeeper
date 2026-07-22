"""Deterministic SVG export for a bounded graph projection (no external deps)."""

from __future__ import annotations

import math
from xml.sax.saxutils import escape


def to_svg(projection: dict, width: int = 960, height: int = 720) -> str:
    nodes = projection.get("nodes", [])
    edges = projection.get("edges", [])
    count = max(1, len(nodes))
    cx, cy = width / 2, height / 2
    radius = min(width, height) / 2 - 80
    positions: dict[str, tuple[float, float]] = {}
    for index, node in enumerate(nodes):
        angle = 2 * math.pi * index / count
        positions[node["id"]] = (cx + radius * math.cos(angle), cy + radius * math.sin(angle))
    lines = []
    for edge in edges:
        source = positions.get(edge["source"])
        target = positions.get(edge["target"])
        if source and target:
            lines.append(
                f'<line x1="{source[0]:.1f}" y1="{source[1]:.1f}" x2="{target[0]:.1f}" y2="{target[1]:.1f}" stroke="#8888" stroke-width="1"/>'
            )
    marks = []
    for node in nodes:
        x, y = positions[node["id"]]
        label = escape(str(node.get("label", ""))[:28])
        marks.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="6" fill="#3377aa"/>'
            f'<text x="{x + 9:.1f}" y="{y + 3:.1f}" font-size="10" font-family="sans-serif">{label}</text>'
        )
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}"><rect width="100%" height="100%" fill="white"/>'
        f'{"".join(lines)}{"".join(marks)}</svg>'
    )
