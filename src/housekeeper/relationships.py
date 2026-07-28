import json

# Cached graph projections are keyed by projection parameters, not by the data, so a relationship
# write has to invalidate them. Doing that inline meant a relationship-heavy stage issued
# `DELETE FROM graph_layout_cache` — and a commit — hundreds of thousands of times. Writers now
# just set this flag; the cache is cleared once, by whoever finishes the stage or reads the graph.
_graph_cache_stale = False


def mark_graph_cache_stale() -> None:
    global _graph_cache_stale
    _graph_cache_stale = True


def invalidate_graph_cache(database) -> bool:
    """Clear cached graph projections if any relationship changed since the last clear.

    Called at the end of every tracked stage and before a projection is served, so no reader can
    see a projection that predates a write, however the writer was invoked.
    """
    global _graph_cache_stale
    if not _graph_cache_stale:
        return False
    database.connect().execute("DELETE FROM graph_layout_cache")
    database.connect().commit()
    _graph_cache_stale = False
    return True


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
    mark_graph_cache_stale()
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
    mark_graph_cache_stale()
    return int(row[0])


def upsert_content_relationship(
    database,
    source_type: str,
    source_id: int,
    target_type: str,
    target_id: int,
    relationship_type: str,
    evidence_tier: str,
    confidence: float,
    algorithm: str,
    algorithm_version: str,
    configuration_fingerprint: str,
    evidence: dict,
    explanation: str,
) -> int:
    """Write a tiered, provenance-carrying relationship into ``content_relationships``.

    Symmetric relationships are stored with a canonical ordering (source_id <= target_id) so a
    pair is never duplicated in both directions. This never touches the legacy ``relationships``
    table, so existing exact-duplicate behavior and the graph are unaffected.
    """
    if not 0 <= confidence <= 1:
        raise ValueError("confidence must be between 0 and 1")
    if source_type == target_type and source_id > target_id:
        source_id, target_id = target_id, source_id
    conn = database.connect()
    conn.execute(
        """INSERT INTO content_relationships(source_type,source_id,target_type,target_id,relationship_type,
           evidence_tier,confidence,algorithm,algorithm_version,configuration_fingerprint,evidence_json,explanation)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source_type,source_id,target_type,target_id,relationship_type,algorithm,algorithm_version,configuration_fingerprint)
           DO UPDATE SET evidence_tier=excluded.evidence_tier,confidence=excluded.confidence,
           evidence_json=excluded.evidence_json,explanation=excluded.explanation,status='ACTIVE',
           updated_at=CURRENT_TIMESTAMP,invalidated_at=NULL""",
        (
            source_type,
            source_id,
            target_type,
            target_id,
            relationship_type,
            evidence_tier,
            confidence,
            algorithm,
            algorithm_version,
            configuration_fingerprint,
            json.dumps(evidence, sort_keys=True),
            explanation,
        ),
    )
    row = conn.execute(
        """SELECT id FROM content_relationships WHERE source_type=? AND source_id=? AND target_type=? AND target_id=?
           AND relationship_type=? AND algorithm=? AND algorithm_version=? AND configuration_fingerprint=?""",
        (
            source_type,
            source_id,
            target_type,
            target_id,
            relationship_type,
            algorithm,
            algorithm_version,
            configuration_fingerprint,
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


def invalidate_content_relationships(
    database, algorithm: str, algorithm_version: str, configuration_fingerprint: str
) -> int:
    """Mark superseded tiered relationships invalid when an algorithm/config version changes."""
    cur = database.connect().execute(
        """UPDATE content_relationships SET status='INVALIDATED',invalidated_at=CURRENT_TIMESTAMP
           WHERE algorithm=? AND (algorithm_version<>? OR configuration_fingerprint<>?) AND status='ACTIVE'""",
        (algorithm, algorithm_version, configuration_fingerprint),
    )
    return cur.rowcount


def invalidate_relationships(
    database,
    relationship_type: str | None = None,
    version: str | None = None,
    except_version: str | None = None,
) -> int:
    """Delete relationships an analyser no longer stands behind.

    ``version`` names a generation to remove; ``except_version`` removes everything *but* one, which
    is what an analyser whose algorithm changed actually wants — it does not have to remember every
    superseded version number, and a generation nobody thought to list cannot survive.
    """
    clauses, params = [], []
    if relationship_type:
        clauses.append("relationship_type=?")
        params.append(relationship_type)
    if version:
        clauses.append("relationship_version=?")
        params.append(version)
    if except_version:
        clauses.append("relationship_version<>?")
        params.append(except_version)
    where = " AND ".join(clauses) or "1=1"
    cur = database.connect().execute(f"DELETE FROM relationships WHERE {where}", tuple(params))
    mark_graph_cache_stale()
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
