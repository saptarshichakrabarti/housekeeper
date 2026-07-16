PROJECTION_TYPES = {
    "universe",
    "backup-lineage",
    "project",
    "duplicate",
    "document-family",
    "image-cluster",
    "selected-directory",
    "content",
}


def validate_projection(projection_type: str) -> str:
    if projection_type not in PROJECTION_TYPES:
        raise ValueError(f"unknown graph projection: {projection_type}")
    return projection_type


def projection_limits(
    config, requested_nodes: int | None = None, requested_edges: int | None = None
) -> tuple[int, int]:
    graph = config.section("graph")
    nodes = requested_nodes or graph["default_max_nodes"]
    edges = requested_edges or graph["default_max_edges"]
    if nodes > graph["hard_max_nodes"] or edges > graph["hard_max_edges"]:
        raise ValueError("requested graph projection exceeds hard limits")
    return nodes, edges
