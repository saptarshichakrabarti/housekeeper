"""Graph projection tests: types, limits, aggregation, caching, evidence."""

import pytest

from housekeeper.graph.builder import build_projection
from housekeeper.graph.projections import PROJECTION_TYPES


def _seed_relationship(database, confidence=0.9):
    database.connect().execute(
        "INSERT INTO relationships(source_type,source_id,target_type,target_id,relationship_type,confidence,evidence_json,relationship_version)"
        " VALUES('DUPLICATE_GROUP',1,'ENTRY',2,'EXACT_DUPLICATE_MEMBER',?,'{\"shared_bytes\": 10}','1')",
        (confidence,),
    )
    database.connect().commit()


def test_every_projection_type_builds(database):
    _seed_relationship(database)
    for projection_type in PROJECTION_TYPES:
        result = build_projection(database, projection_type, max_nodes=50, max_edges=50)
        assert "nodes" in result and "edges" in result


def test_selected_directory_projection_does_not_error(database):
    # Regression: 'selected-directory' validated but had no clause (KeyError/500).
    result = build_projection(database, "selected-directory", max_nodes=10, max_edges=10)
    assert isinstance(result["nodes"], list)


def test_node_and_edge_limits_enforced(database):
    for i in range(20):
        database.connect().execute(
            "INSERT INTO relationships(source_type,source_id,target_type,target_id,relationship_type,confidence,relationship_version)"
            " VALUES('ENTRY',?,'ENTRY',?,'DUPLICATE_CONTENT',0.95,'1')",
            (i, i + 100),
        )
    database.connect().commit()
    result = build_projection(database, "duplicate", max_nodes=3, max_edges=3)
    assert len(result["nodes"]) <= 3
    assert len(result["edges"]) <= 3
    assert result["truncated"] is True


def test_confidence_filter_excludes_weak_edges(database):
    _seed_relationship(database, confidence=0.5)
    result = build_projection(database, "duplicate", minimum_confidence=0.9, max_nodes=50, max_edges=50)
    assert result["edges"] == []


def test_invalid_projection_type_raises(database):
    with pytest.raises(ValueError):
        build_projection(database, "not-a-real-projection")


def test_universe_aggregates_into_clusters(config, database, tmp_path):
    from housekeeper.scanner import DriveScanner

    root = tmp_path / "src"
    (root / "Photos").mkdir(parents=True)
    for i in range(5):
        (root / "Photos" / f"p{i}.bin").write_bytes(bytes([i]) * 10)
    DriveScanner(database, config).scan(root, incremental=False)
    result = build_projection(database, "universe", max_nodes=50, max_edges=50)
    node_types = {node["node_type"] for node in result["nodes"]}
    assert "DIRECTORY_CLUSTER" in node_types  # never renders raw files
    assert result["aggregation_applied"] is True


def test_layout_cache_is_used(database):
    _seed_relationship(database)
    build_projection(database, "duplicate", max_nodes=10, max_edges=10)
    cached = database.fetch_one("SELECT COUNT(*) n FROM graph_layout_cache")["n"]
    assert cached >= 1
