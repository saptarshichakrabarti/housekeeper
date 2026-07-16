import json


def replace_relationship_group(
    database,
    group_type: str,
    group_key: str,
    content_object_ids: list[int],
    evidence: dict,
    version: str = "1",
) -> int:
    """Persist a normalized, idempotent group rather than only pairwise edges."""
    if len(set(content_object_ids)) < 2:
        raise ValueError("relationship groups require at least two content objects")
    conn = database.connect()
    conn.execute(
        """INSERT INTO relationship_groups(group_type,group_key,relationship_version,evidence_json)
           VALUES(?,?,?,?) ON CONFLICT(group_type,group_key,relationship_version)
           DO UPDATE SET evidence_json=excluded.evidence_json,updated_at=CURRENT_TIMESTAMP""",
        (group_type, group_key, version, json.dumps(evidence, sort_keys=True)),
    )
    row = conn.execute(
        "SELECT id FROM relationship_groups WHERE group_type=? AND group_key=? AND relationship_version=?",
        (group_type, group_key, version),
    ).fetchone()
    assert row is not None
    group_id = int(row[0])
    conn.execute("DELETE FROM relationship_group_members WHERE group_id=?", (group_id,))
    conn.executemany(
        "INSERT INTO relationship_group_members(group_id,content_object_id) VALUES(?,?)",
        [(group_id, value) for value in sorted(set(content_object_ids))],
    )
    conn.execute("DELETE FROM graph_layout_cache")
    conn.commit()
    return group_id


def upsert_relationship(
    database,
    source_type: str,
    source_id: int,
    target_type: str,
    target_id: int,
    relationship_type: str,
    confidence: float,
    evidence: dict,
    version: str = "1",
) -> int:
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    conn = database.connect()
    conn.execute(
        """INSERT INTO relationships(source_type,source_id,target_type,target_id,relationship_type,confidence,evidence_json,relationship_version)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source_type,source_id,target_type,target_id,relationship_type,relationship_version)
        DO UPDATE SET confidence=excluded.confidence,evidence_json=excluded.evidence_json""",
        (
            source_type,
            source_id,
            target_type,
            target_id,
            relationship_type,
            confidence,
            json.dumps(evidence, sort_keys=True),
            version,
        ),
    )
    row = conn.execute(
        "SELECT id FROM relationships WHERE source_type=? AND source_id=? AND target_type=? AND target_id=? AND relationship_type=? AND relationship_version=?",
        (source_type, source_id, target_type, target_id, relationship_type, version),
    ).fetchone()
    conn.execute("DELETE FROM graph_layout_cache")
    conn.commit()
    return int(row[0])


def invalidate_relationships(
    database, relationship_type: str | None = None, version: str | None = None
) -> int:
    clauses, params = [], []
    if relationship_type:
        clauses.append("relationship_type=?")
        params.append(relationship_type)
    if version:
        clauses.append("relationship_version=?")
        params.append(version)
    where = " AND ".join(clauses) or "1=1"
    cur = database.connect().execute(f"DELETE FROM relationships WHERE {where}", tuple(params))
    database.connect().execute("DELETE FROM graph_layout_cache")
    database.connect().commit()
    return cur.rowcount


def get_neighbors(database, node_type: str, node_id: int, minimum_confidence: float = 0.0):
    return database.fetch_all(
        """SELECT * FROM relationships WHERE confidence>=? AND ((source_type=? AND source_id=?) OR (target_type=? AND target_id=?)) ORDER BY confidence DESC""",
        (minimum_confidence, node_type, node_id, node_type, node_id),
    )


def get_subgraph(
    database, node_type: str, node_id: int, depth: int = 1, minimum_confidence: float = 0.0
):
    if depth < 0 or depth > 5:
        raise ValueError("depth must be between 0 and 5")
    seen = {(node_type, node_id)}
    frontier = set(seen)
    edges = []
    for _ in range(depth):
        next_frontier = set()
        for typ, ident in frontier:
            for row in get_neighbors(database, typ, ident, minimum_confidence):
                edges.append(row)
                other = (
                    (row["target_type"], row["target_id"])
                    if (row["source_type"], row["source_id"]) == (typ, ident)
                    else (row["source_type"], row["source_id"])
                )
                if other not in seen:
                    next_frontier.add(other)
        seen.update(next_frontier)
        frontier = next_frontier
    return edges
