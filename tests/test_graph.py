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


# ------------------------------------------------------------------- lazy explorer
# The Obsidian-style dashboard view: the first payload is only the scanned source roots, and
# every later request reveals exactly one clicked node's immediate children.


def _explorer_tree(config, database, tmp_path):
    from housekeeper.scanner import DriveScanner

    root = tmp_path / "drive"
    (root / "Photos" / "2024").mkdir(parents=True)
    (root / "Docs").mkdir()
    (root / "Photos" / "2024" / "a.jpg").write_bytes(b"same-bytes")
    (root / "Photos" / "2024" / "b.jpg").write_bytes(b"same-bytes")  # exact duplicates
    (root / "Docs" / "note.txt").write_text("unique", encoding="utf-8")
    (root / "top.txt").write_text("top-level file", encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    return root


def _node(payload, label):
    return next(node for node in payload["nodes"] if node["label"] == label)


def test_explorer_starts_with_collapsed_roots(config, database, tmp_path):
    from housekeeper.graph.explorer import build_explorer

    _explorer_tree(config, database, tmp_path)
    payload = build_explorer(database)
    assert {node["node_type"] for node in payload["nodes"]} == {"SOURCE_ROOT"}
    assert payload["edges"] == []  # nothing pre-expanded
    root = payload["nodes"][0]
    assert root["attributes"]["expandable"] is True
    assert root["attributes"]["child_count"] == 3  # Photos, Docs, top.txt


def test_explorer_expands_one_level_at_a_time(config, database, tmp_path):
    from housekeeper.graph.explorer import build_explorer

    _explorer_tree(config, database, tmp_path)
    root_node = build_explorer(database)["nodes"][0]["id"]
    level_one = build_explorer(database, root_node)
    labels = {node["label"] for node in level_one["nodes"]}
    assert labels == {"Photos", "Docs", "top.txt"}  # children only — grandchildren stay hidden
    photos = _node(level_one, "Photos")
    assert photos["node_type"] == "DIRECTORY"
    assert photos["attributes"]["expandable"] is True  # contains 2024
    assert all(edge["source"] == root_node for edge in level_one["edges"])
    level_two = build_explorer(database, photos["id"])
    assert {node["label"] for node in level_two["nodes"]} == {"2024"}


def test_explorer_marks_duplicate_files(config, database, tmp_path):
    from housekeeper.analysers.exact_duplicates import run_exact_duplicate_analysis
    from housekeeper.graph.explorer import build_explorer

    _explorer_tree(config, database, tmp_path)
    run_exact_duplicate_analysis(database, config)
    root_node = build_explorer(database)["nodes"][0]["id"]
    photos = _node(build_explorer(database, root_node), "Photos")
    year = _node(build_explorer(database, photos["id"]), "2024")
    files = build_explorer(database, year["id"])
    assert {node["attributes"]["duplicate"] for node in files["nodes"]} == {True}
    assert {node["node_type"] for node in files["nodes"]} == {"FILE"}


def test_explorer_truncates_with_honest_overflow(config, database, tmp_path):
    from housekeeper.graph.explorer import build_explorer

    _explorer_tree(config, database, tmp_path)
    root_node = build_explorer(database)["nodes"][0]["id"]
    payload = build_explorer(database, root_node, limit=2)
    assert payload["truncated"] is True
    overflow = next(node for node in payload["nodes"] if node["node_type"] == "OVERFLOW")
    assert overflow["attributes"]["member_count"] == 1  # 3 children, 2 shown
    assert overflow["attributes"]["expandable"] is False


def test_explorer_rejects_malformed_and_unknown_nodes(config, database, tmp_path):
    from housekeeper.graph.explorer import build_explorer

    _explorer_tree(config, database, tmp_path)
    for bad in ("ENTRY:not-a-number", "OVERFLOW:ENTRY:1", "../../etc/passwd", "ENTRY:; DROP"):
        with pytest.raises(ValueError):
            build_explorer(database, bad)
    with pytest.raises(ValueError):
        build_explorer(database, "ENTRY:999999")  # unknown id
    # A file is terminal: asking for its children is an error, not an empty page.
    root_node = build_explorer(database)["nodes"][0]["id"]
    top_txt = _node(build_explorer(database, root_node), "top.txt")
    with pytest.raises(ValueError):
        build_explorer(database, top_txt["id"])


def test_explorer_endpoint_is_bounded_and_validated(config, database, tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from housekeeper.dashboard.app import create_app

    _explorer_tree(config, database, tmp_path)
    client = TestClient(create_app(database))
    roots = client.get("/api/graph/children").json()
    assert roots["nodes"] and roots["nodes"][0]["node_type"] == "SOURCE_ROOT"
    assert client.get("/api/graph/children?node=bogus").status_code == 422
    assert client.get("/api/graph/children?limit=999999").status_code == 422
