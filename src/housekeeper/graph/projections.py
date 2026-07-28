PROJECTION_TYPES = {
    "universe",
    "backup-lineage",
    "project",
    "duplicate",
    "document-family",
    "image-cluster",
    "selected-directory",
    "content",
    "content-equivalence",
    "partial-overlap",
    "derivation-family",
}


def validate_projection(projection_type: str) -> str:
    if projection_type not in PROJECTION_TYPES:
        raise ValueError(f"unknown graph projection: {projection_type}")
    return projection_type


def graph_settings(config) -> dict:
    """The ``graph`` section, or its defaults when no config was threaded through."""
    if config is not None:
        return config.section("graph")
    from ..config import DEFAULTS

    return DEFAULTS["graph"]


def projection_limits(
    config, requested_nodes: int | None = None, requested_edges: int | None = None
) -> tuple[int, int]:
    """Resolve a projection's size against the configured defaults and hard ceilings.

    These four keys used to be read only by this function, which nothing called: the builder
    carried its own hard-coded 5,000/20,000 and ignored the configuration entirely.
    """
    graph = graph_settings(config)
    nodes = int(requested_nodes or graph["default_max_nodes"])
    edges = int(requested_edges or graph["default_max_edges"])
    if nodes < 1 or edges < 1:
        raise ValueError("graph limits must be positive")
    if nodes > graph["hard_max_nodes"] or edges > graph["hard_max_edges"]:
        raise ValueError("requested graph projection exceeds hard limits")
    return nodes, edges
