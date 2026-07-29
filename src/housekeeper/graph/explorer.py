"""Lazy, folder-by-folder graph exploration over the current inventory.

The explorer serves the dashboard's Obsidian-style graph view: the initial payload is only the
scanned source roots (collapsed), and every subsequent request returns the immediate children of
one node the user clicked. Nothing is ever expanded server-side beyond a single level, so the
response size is bounded by ``limit`` regardless of inventory size.

Node identity reuses the relationship-store conventions (``SOURCE_ROOT:<id>``, ``ENTRY:<id>``) so
a node found here can be handed to the existing relationship projections unchanged. Requests are
validated against a strict pattern — this endpoint never accepts paths or free-form SQL inputs.
"""

from __future__ import annotations

import re

from .model import GraphEdge, GraphNode, serialize

# The only node shapes the explorer can expand. OVERFLOW leaves are terminal by construction.
_NODE_PATTERN = re.compile(r"^(SOURCE_ROOT|ENTRY):(\d{1,18})$")


def _duplicate_entry_ids(database, entry_ids: list[int]) -> set[int]:
    """The subset of ``entry_ids`` that belongs to a current exact-duplicate group."""
    if not entry_ids:
        return set()
    marks = ",".join("?" for _ in entry_ids)
    rows = database.fetch_all(
        f"SELECT entry_id FROM current_exact_duplicate_members WHERE entry_id IN ({marks})",
        tuple(entry_ids),
    )
    return {int(row["entry_id"]) for row in rows}


def _child_directory_counts(database, entry_ids: list[int]) -> dict[int, int]:
    """How many current children each of ``entry_ids`` has, in one grouped query."""
    if not entry_ids:
        return {}
    marks = ",".join("?" for _ in entry_ids)
    rows = database.fetch_all(
        f"SELECT parent_entry_id p, COUNT(*) n FROM current_entries "
        f"WHERE parent_entry_id IN ({marks}) GROUP BY parent_entry_id",
        tuple(entry_ids),
    )
    return {int(row["p"]): int(row["n"]) for row in rows}


def _roots(database, limit: int) -> dict:
    """The collapsed starting view: one node per scanned source root, no edges."""
    rows = database.fetch_all(
        """SELECT s.id, s.display_name, s.last_mount_path,
                  (SELECT COUNT(*) FROM current_entries e
                   WHERE e.source_root_id=s.id AND e.parent_entry_id IS NULL) AS child_count
           FROM source_roots s
           WHERE s.latest_complete_scan_run_id IS NOT NULL
           ORDER BY s.id LIMIT ?""",
        (limit + 1,),
    )
    truncated = len(rows) > limit
    nodes = [
        GraphNode(
            f"SOURCE_ROOT:{int(row['id'])}",
            "SOURCE_ROOT",
            str(row["display_name"] or row["last_mount_path"]),
            {
                "entity_id": int(row["id"]),
                "child_count": int(row["child_count"]),
                "expandable": int(row["child_count"]) > 0,
                "path": str(row["last_mount_path"] or ""),
            },
        )
        for row in rows[:limit]
    ]
    return serialize(nodes, [], {"type": "explorer", "node": None, "limit": limit}, truncated)


def _children_rows(database, node_type: str, node_id: int, limit: int) -> list:
    if node_type == "SOURCE_ROOT":
        where, params = "e.source_root_id=? AND e.parent_entry_id IS NULL", (node_id,)
        anchor = database.fetch_one("SELECT id FROM source_roots WHERE id=?", (node_id,))
    else:
        where, params = "e.parent_entry_id=?", (node_id,)
        anchor = database.fetch_one(
            "SELECT id FROM current_entries WHERE id=? AND entry_type='directory'", (node_id,)
        )
    if not anchor:
        raise ValueError("unknown or non-expandable node")
    # Directories first (they are what the user explores), then largest files — the ordering that
    # keeps a truncated view useful.
    return database.fetch_all(
        f"""SELECT e.id, e.name, e.entry_type, e.size_bytes, e.suffix, e.relative_path
            FROM current_entries e WHERE {where}
            ORDER BY (e.entry_type!='directory'), e.size_bytes DESC, e.name LIMIT ?""",
        (*params, limit + 1),
    )


def build_explorer(database, node: str | None = None, limit: int = 150) -> dict:
    """One level of the lazy explorer: source roots when ``node`` is None, else its children."""
    limit = max(1, int(limit))
    if node is None:
        return _roots(database, limit)
    match = _NODE_PATTERN.match(node)
    if not match:
        raise ValueError("node must look like SOURCE_ROOT:<id> or ENTRY:<id>")
    node_type, node_id = match.group(1), int(match.group(2))
    rows = _children_rows(database, node_type, node_id, limit)
    truncated = len(rows) > limit
    rows = rows[:limit]
    directory_ids = [int(row["id"]) for row in rows if row["entry_type"] == "directory"]
    file_ids = [int(row["id"]) for row in rows if row["entry_type"] != "directory"]
    grandchildren = _child_directory_counts(database, directory_ids)
    duplicates = _duplicate_entry_ids(database, file_ids)
    parent_node = f"{node_type}:{node_id}"
    nodes: list[GraphNode] = []
    edges: list[GraphEdge] = []
    for row in rows:
        entry_id = int(row["id"])
        is_directory = row["entry_type"] == "directory"
        child_count = grandchildren.get(entry_id, 0)
        node_id_str = f"ENTRY:{entry_id}"
        nodes.append(
            GraphNode(
                node_id_str,
                "DIRECTORY" if is_directory else "FILE",
                str(row["name"]),
                {
                    "entity_id": entry_id,
                    "entry_type": str(row["entry_type"]),
                    "size_bytes": int(row["size_bytes"] or 0),
                    "child_count": child_count,
                    "expandable": is_directory and child_count > 0,
                    "duplicate": entry_id in duplicates,
                    "suffix": str(row["suffix"] or ""),
                    "path": str(row["relative_path"]),
                },
            )
        )
        edges.append(
            GraphEdge(
                f"contains:{parent_node}:{node_id_str}",
                parent_node,
                node_id_str,
                "CONTAINS",
                1.0,
                {},
            )
        )
    if truncated:
        # An honest terminal marker: the remainder exists but is not rendered. Counted exactly so
        # the label never lies about coverage.
        total = database.fetch_one(
            "SELECT COUNT(*) n FROM current_entries e WHERE "
            + ("e.source_root_id=? AND e.parent_entry_id IS NULL" if node_type == "SOURCE_ROOT" else "e.parent_entry_id=?"),
            (node_id,),
        )
        remainder = max(0, int(total["n"] if total else 0) - limit)
        overflow_id = f"OVERFLOW:{parent_node}"
        nodes.append(
            GraphNode(
                overflow_id,
                "OVERFLOW",
                f"+{remainder:,} more",
                {"member_count": remainder, "expandable": False},
            )
        )
        edges.append(
            GraphEdge(f"contains:{parent_node}:{overflow_id}", parent_node, overflow_id, "CONTAINS", 1.0, {"aggregate": True})
        )
    return serialize(
        nodes, edges, {"type": "explorer", "node": node, "limit": limit}, truncated
    )
