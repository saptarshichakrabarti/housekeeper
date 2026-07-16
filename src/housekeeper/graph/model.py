from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class GraphNode:
    id: str
    node_type: str
    label: str
    attributes: dict[str, Any]


@dataclass(frozen=True)
class GraphEdge:
    id: str
    source: str
    target: str
    edge_type: str
    confidence: float
    evidence: dict[str, Any]


def serialize(
    nodes: list[GraphNode],
    edges: list[GraphEdge],
    projection: dict[str, Any],
    truncated: bool = False,
) -> dict[str, Any]:
    return {
        "projection": projection,
        "nodes": [n.__dict__ for n in nodes],
        "edges": [e.__dict__ for e in edges],
        "truncated": truncated,
        "aggregation_applied": any(n.node_type.endswith("_CLUSTER") for n in nodes),
        "warnings": [],
    }
