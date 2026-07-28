"""Bounded, cacheable graph projections over the relationship store.

The graph is intentionally a projection: authoritative detail remains available through
the structured explorer APIs.  This keeps an accidental million-node rendering from
turning into a browser or database denial of service.
"""

import hashlib
import json

from ..relationships import get_subgraph
from .model import GraphEdge, GraphNode, serialize


def _projection_clause(projection_type: str) -> tuple[str, tuple[object, ...]]:
    clauses = {
        "universe": ("1=1", ()),
        "duplicate": ("relationship_type IN ('EXACT_DUPLICATE_MEMBER','DUPLICATE_CONTENT')", ()),
        "content": ("source_type='CONTENT_OBJECT' OR target_type='CONTENT_OBJECT'", ()),
        "backup-lineage": ("relationship_type LIKE '%BACKUP%'", ()),
        "project": ("relationship_type LIKE '%PROJECT%'", ()),
        "document-family": ("relationship_type LIKE '%DOCUMENT%'", ()),
        "image-cluster": ("relationship_type LIKE '%IMAGE%'", ()),
        # A directory-scoped exploration: unrestricted relationship type, narrowed by the
        # supplied root_id/depth via get_subgraph.  Falls back to a general view without a root.
        "selected-directory": ("1=1", ()),
    }
    return clauses[projection_type]


def _labels(database, identifiers: set[tuple[str, int]]) -> dict[tuple[str, int], str]:
    """Resolve only the displayed nodes, grouped by backing table."""
    result = {item: f"{item[0]} {item[1]}" for item in identifiers}
    table_specs = {
        "ENTRY": ("filesystem_entries", "id", "name"),
        "CONTENT_OBJECT": ("content_objects", "id", "full_hash"),
        "DUPLICATE_GROUP": ("exact_duplicate_groups", "id", "full_hash"),
        "SOURCE_ROOT": ("source_roots", "id", "display_name"),
        "PROJECT": ("projects", "id", "name"),
    }
    for node_type, (table, ident, label) in table_specs.items():
        ids = [node_id for typ, node_id in identifiers if typ == node_type]
        if not ids:
            continue
        marks = ",".join("?" for _ in ids)
        for row in database.fetch_all(f"SELECT {ident},{label} FROM {table} WHERE {ident} IN ({marks})", tuple(ids)):
            result[(node_type, int(row[ident]))] = str(row[label])
    return result


def _relationship_version(database) -> str:
    row = database.fetch_one("SELECT COALESCE(MAX(id),0) AS n,COUNT(*) AS count FROM relationships")
    return f"{row['n']}:{row['count']}" if row else "0:0"


def _cache_key(*parts: object) -> str:
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()


def _universe_aggregation(database, max_nodes: int, max_edges: int, aggregation_level: str) -> dict:
    """Strategic default: source roots plus top-level aggregate clusters, never raw files."""
    if aggregation_level not in {"auto", "source", "directory"}:
        raise ValueError("aggregation_level must be auto, source, directory, or none")
    rows = database.fetch_all(
        """SELECT source_root,
           CASE WHEN instr(relative_path,'/')=0 THEN relative_path ELSE substr(relative_path,1,instr(relative_path,'/')-1) END AS top_level,
           COUNT(*) AS member_count,COALESCE(SUM(size_bytes),0) AS size_bytes
           FROM filesystem_entries WHERE entry_type='file'
           GROUP BY source_root,top_level ORDER BY size_bytes DESC LIMIT ?""",
        (max_nodes,),
    )
    roots = database.fetch_all("SELECT id,display_name,last_mount_path FROM source_roots ORDER BY id")
    root_ids = {str(row["last_mount_path"]): int(row["id"]) for row in roots}
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    seen_roots: set[str] = set()
    for row in rows:
        source = str(row["source_root"])
        root_id = root_ids.get(source)
        root_node = f"SOURCE_ROOT:{root_id}" if root_id else f"SOURCE_ROOT:path:{hashlib.sha1(source.encode()).hexdigest()[:12]}"
        if root_node not in seen_roots:
            nodes.append(GraphNode(root_node, "SOURCE_ROOT", next((str(root["display_name"]) for root in roots if root_ids.get(str(root["last_mount_path"])) == root_id), source), {"source_root": source}))
            seen_roots.add(root_node)
        cluster_id = f"DIRECTORY_CLUSTER:{hashlib.sha1((source + '/' + str(row['top_level'])).encode()).hexdigest()[:16]}"
        nodes.append(GraphNode(cluster_id, "DIRECTORY_CLUSTER", str(row["top_level"] or "/"), {"source_root": source, "member_count": int(row["member_count"]), "size_bytes": int(row["size_bytes"])}))
        edges.append(GraphEdge(f"contains:{cluster_id}", root_node, cluster_id, "CONTAINS", 1.0, {"aggregate": True, "member_count": int(row["member_count"])}))
    return serialize(nodes[:max_nodes], edges[:max_edges], {"type": "universe", "aggregation_level": aggregation_level, "max_nodes": max_nodes, "max_edges": max_edges}, len(nodes) > max_nodes or len(edges) > max_edges)


_CONTENT_PROJECTION_FILTER = {
    "content-equivalence": "evidence_tier IN ('TIER_1_EXACT','TIER_2_NORMALIZED_EXACT','TIER_3_STRONG_EQUIVALENCE')",
    "partial-overlap": "evidence_tier='TIER_4_PARTIAL_OVERLAP'",
    "derivation-family": "relationship_type IN ('LIKELY_EXPORT','LIKELY_RENDERING','LIKELY_DERIVED','LIKELY_VERSION','LIKELY_COPY')",
}


def _content_relationship_projection(
    database, projection_type: str, max_nodes: int, max_edges: int, minimum_confidence: float
) -> dict:
    """A bounded projection over the tiered ``content_relationships`` store."""
    where = _CONTENT_PROJECTION_FILTER[projection_type]
    rows = database.fetch_all(
        f"""SELECT id,source_type,source_id,target_type,target_id,relationship_type,evidence_tier,confidence,evidence_json
            FROM content_relationships WHERE status='ACTIVE' AND ({where}) AND confidence>=?
            ORDER BY confidence DESC,id LIMIT ?""",
        (minimum_confidence, max_edges + 1),
    )
    truncated = len(rows) > max_edges
    rows = rows[:max_edges]
    identifiers: set[tuple[str, int]] = set()
    usable = []
    for row in rows:
        a = (str(row["source_type"]), int(row["source_id"]))
        b = (str(row["target_type"]), int(row["target_id"]))
        if a not in identifiers and len(identifiers) >= max_nodes:
            truncated = True
            continue
        identifiers.add(a)
        if b not in identifiers and len(identifiers) >= max_nodes:
            truncated = True
            continue
        identifiers.add(b)
        usable.append(row)
    labels = _labels(database, identifiers)
    nodes = [
        GraphNode(f"{typ}:{ident}", typ, labels.get((typ, ident), f"{typ} {ident}"), {"entity_id": ident})
        for typ, ident in sorted(identifiers)
    ]
    allowed = {node.id for node in nodes}
    edges = [
        GraphEdge(
            f"content_rel:{row['id']}",
            f"{row['source_type']}:{row['source_id']}",
            f"{row['target_type']}:{row['target_id']}",
            str(row["relationship_type"]),
            float(row["confidence"]),
            {"evidence_tier": row["evidence_tier"], **json.loads(row["evidence_json"] or "{}")},
        )
        for row in usable
        if f"{row['source_type']}:{row['source_id']}" in allowed
        and f"{row['target_type']}:{row['target_id']}" in allowed
    ]
    return serialize(
        nodes,
        edges,
        {"type": projection_type, "max_nodes": max_nodes, "max_edges": max_edges},
        truncated,
    )


def build_projection(
    database,
    projection_type: str = "universe",
    root_id: int | None = None,
    root_type: str | None = None,
    depth: int = 1,
    max_nodes: int | None = None,
    max_edges: int | None = None,
    minimum_confidence: float | None = None,
    aggregation_level: str = "auto",
    include_types: tuple[str, ...] = (),
    exclude_types: tuple[str, ...] = (),
    config=None,
):
    """Build one bounded projection. Limits and the confidence floor come from ``config``.

    ``None`` for any of the three means "use the configured value"; the builder previously carried
    its own hard-coded 500/2,000/0.7 and a 5,000/20,000 ceiling, so the four ``graph.*_max_*`` keys
    and ``graph.minimum_edge_confidence`` were settings nothing consulted.
    """
    from .projections import graph_settings, projection_limits, validate_projection

    validate_projection(projection_type)
    max_nodes, max_edges = projection_limits(config, max_nodes, max_edges)
    if minimum_confidence is None:
        minimum_confidence = float(graph_settings(config)["minimum_edge_confidence"])
    if not 0 <= minimum_confidence <= 1 or not 1 <= depth <= 5:
        raise ValueError("confidence must be 0..1 and depth must be 1..5")
    # Relationship writers only flag the cache stale; the clear happens here (and at the end of
    # each tracked stage) so no reader can be served a projection that predates a write.
    from ..relationships import invalidate_graph_cache

    invalidate_graph_cache(database)
    version = _relationship_version(database)
    key = _cache_key(projection_type, root_type, root_id, depth, max_nodes, max_edges, minimum_confidence, aggregation_level, include_types, exclude_types, version)
    cached = load_cached_projection(database, key)
    if cached:
        return cached["projection"]

    if projection_type in {"content-equivalence", "partial-overlap", "derivation-family"}:
        projection = _content_relationship_projection(
            database, projection_type, max_nodes, max_edges, minimum_confidence
        )
        cache_projection(database, key, projection, {}, version)
        return projection

    if projection_type == "universe" and root_id is None and aggregation_level != "none":
        projection = _universe_aggregation(database, max_nodes, max_edges, aggregation_level)
        cache_projection(database, key, projection, {}, version)
        return projection

    where, parameters = _projection_clause(projection_type)
    if root_id is not None:
        if not root_type:
            raise ValueError("root_type is required when root_id is supplied")
        rows = list(get_subgraph(database, root_type, root_id, depth, minimum_confidence))
        # Apply projection semantics to expanded relationships too.
        if where != "1=1":
            allowed = database.fetch_all(
                f"SELECT id FROM relationships WHERE ({where}) AND confidence>=?", (*parameters, minimum_confidence)
            )
            allowed_ids = {int(row["id"]) for row in allowed}
            rows = [row for row in rows if int(row["id"]) in allowed_ids]
    else:
        rows = database.fetch_all(
            f"SELECT * FROM relationships WHERE ({where}) AND confidence>=? ORDER BY confidence DESC,id LIMIT ?",
            (*parameters, minimum_confidence, max_edges + 1),
        )
    truncated = len(rows) > max_edges
    rows = rows[:max_edges]
    if include_types:
        rows = [row for row in rows if str(row["relationship_type"]) in include_types]
    if exclude_types:
        rows = [row for row in rows if str(row["relationship_type"]) not in exclude_types]
    identifiers: set[tuple[str, int]] = set()
    usable_rows = []
    for row in rows:
        pair = ((str(row["source_type"]), int(row["source_id"])), (str(row["target_type"]), int(row["target_id"])))
        if pair[0] not in identifiers and len(identifiers) >= max_nodes:
            truncated = True
            continue
        identifiers.add(pair[0])
        if pair[1] not in identifiers and len(identifiers) >= max_nodes:
            identifiers.discard(pair[0])
            truncated = True
            continue
        identifiers.add(pair[1])
        usable_rows.append(row)
    if root_id is not None and root_type and len(identifiers) < max_nodes:
        identifiers.add((root_type, root_id))
    if not identifiers and root_id is None:
        # Empty relationship stores should still provide useful entry points for progressive
        # exploration instead of presenting a blank graph.
        seeds = {
            "universe": ("SOURCE_ROOT", "source_roots"),
            "duplicate": ("DUPLICATE_GROUP", "exact_duplicate_groups"),
            "content": ("CONTENT_OBJECT", "content_objects"),
            "image-cluster": ("CONTENT_OBJECT", "content_objects"),
            "project": ("PROJECT", "projects"),
        }
        if projection_type in seeds:
            node_type, table = seeds[projection_type]
            for row in database.fetch_all(f"SELECT id FROM {table} ORDER BY id LIMIT ?", (max_nodes,)):
                identifiers.add((node_type, int(row["id"])))
    labels = _labels(database, identifiers)
    nodes = [GraphNode(f"{typ}:{ident}", typ, labels[(typ, ident)], {"entity_id": ident}) for typ, ident in sorted(identifiers)]
    allowed = {node.id for node in nodes}
    edges = [
        GraphEdge(f"relationship:{row['id']}", f"{row['source_type']}:{row['source_id']}", f"{row['target_type']}:{row['target_id']}", str(row["relationship_type"]), float(row["confidence"]), json.loads(row["evidence_json"] or "{}"))
        for row in usable_rows
        if f"{row['source_type']}:{row['source_id']}" in allowed and f"{row['target_type']}:{row['target_id']}" in allowed
    ]
    projection = serialize(nodes, edges, {"type": projection_type, "root_id": root_id, "root_type": root_type, "depth": depth, "max_nodes": max_nodes, "max_edges": max_edges, "relationship_version": version}, truncated)
    cache_projection(database, key, projection, {}, version)
    return projection


def cache_projection(database, cache_key: str, projection: dict, layout: dict, relationship_version: str = "1") -> None:
    database.connect().execute(
        "INSERT OR REPLACE INTO graph_layout_cache(cache_key,projection_json,layout_json,relationship_version) VALUES(?,?,?,?)",
        (cache_key, json.dumps(projection, sort_keys=True), json.dumps(layout, sort_keys=True), relationship_version),
    )
    database.connect().commit()


def load_cached_projection(database, cache_key: str):
    row = database.fetch_one("SELECT projection_json,layout_json FROM graph_layout_cache WHERE cache_key=?", (cache_key,))
    return {"projection": json.loads(row[0]), "layout": json.loads(row[1])} if row else None
