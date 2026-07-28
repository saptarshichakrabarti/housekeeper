"""Advanced dashboard explorer pages and graph SVG export."""

import pytest

from housekeeper.database import Database
from housekeeper.graph.builder import build_projection
from housekeeper.graph.serialization import to_svg


def _seed(db):
    conn = db.connect()
    conn.execute("INSERT INTO scan_runs(source_root,source_root_fingerprint,status) VALUES('/x','x','COMPLETE')")
    conn.execute(
        """INSERT INTO filesystem_entries(
             id,scan_run_id,source_root,absolute_path,relative_path,name,entry_type,size_bytes)
           VALUES(1,1,'/x','/x/a.txt','a.txt','a.txt','file',1),
                 (2,1,'/x','/x/b.txt','b.txt','b.txt','file',2)"""
    )
    conn.execute("INSERT INTO content_objects(hash_algorithm,full_hash,size_bytes) VALUES('sha256','a',1),('sha256','b',2)")
    conn.execute(
        """INSERT INTO entry_content_links(entry_id,content_object_id,link_status)
           VALUES(1,1,'VERIFIED'),(2,2,'VERIFIED')"""
    )
    conn.execute(
        "INSERT INTO content_relationships(source_type,source_id,target_type,target_id,relationship_type,evidence_tier,confidence,algorithm,algorithm_version,explanation) VALUES"
        "('CONTENT_OBJECT',1,'CONTENT_OBJECT',2,'PIXEL_IDENTICAL','TIER_2_NORMALIZED_EXACT',1.0,'x','1','same pixels'),"
        "('CONTENT_OBJECT',1,'CONTENT_OBJECT',2,'LIKELY_EXPORT','TIER_6_CONTEXTUAL_INFERENCE',0.8,'y','1','docx to pdf')"
    )
    conn.execute("INSERT INTO content_overlap_results(content_object_a_id,content_object_b_id,chunking_profile_id,shared_chunk_count,shared_chunk_bytes,a_total_chunk_bytes,b_total_chunk_bytes,overlap_a_in_b,overlap_b_in_a,weighted_jaccard,confidence) VALUES(1,2,1,3,3000,3200,3400,0.94,0.88,0.85,0.94)")
    conn.execute("INSERT INTO collection_clusters(cluster_type,name) VALUES('PHOTO_EVENT','event-0001')")
    conn.execute(
        "INSERT INTO collection_members(cluster_id,member_type,member_id) VALUES(1,'ENTRY',1)"
    )
    conn.execute("INSERT INTO record_series(name) VALUES('SOURCE_CODE')")
    conn.execute("INSERT INTO preservation_assessments(target_type,target_id,recommended_action,encryption_risk) VALUES('ENTRY',1,'NEEDS_KEY_DOCUMENTATION','high')")
    conn.execute("INSERT INTO review_learning_models(model_type,model_version,feature_schema_version,training_count,active) VALUES('logistic_regression','1','1',42,1)")
    db.refresh_current_inventory_views()  # the scanner does this; a raw-SQL seed must too
    conn.commit()


@pytest.fixture
def client(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from housekeeper.dashboard.app import create_app

    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    _seed(db)
    return TestClient(create_app(db))


@pytest.mark.parametrize(
    "path,marker",
    [
        ("/advanced-duplicates", "PIXEL_IDENTICAL"),
        ("/chunk-overlap", "3000"),
        ("/derivations", "LIKELY_EXPORT"),
        ("/events", "event-0001"),
        ("/record-series", "SOURCE_CODE"),
        ("/preservation", "NEEDS_KEY_DOCUMENTATION"),
        ("/learning", "logistic_regression"),
    ],
)
def test_advanced_pages_render(client, path, marker):
    response = client.get(path)
    assert response.status_code == 200
    assert marker in response.text


def test_advanced_pages_in_navigation(client):
    overview = client.get("/").text
    for link in ("advanced-duplicates", "chunk-overlap", "derivations", "preservation", "learning"):
        assert link in overview


def test_advanced_pages_escape_content(tmp_path):
    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient

    from housekeeper.dashboard.app import create_app

    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    _seed(db)
    db.connect().execute(
        "UPDATE content_relationships SET explanation='<script>alert(1)</script>' "
        "WHERE relationship_type='PIXEL_IDENTICAL'"
    )
    db.connect().commit()
    client = TestClient(create_app(db))
    text = client.get("/advanced-duplicates").text
    assert "<script>alert(1)</script>" not in text
    assert "&lt;script&gt;" in text


def test_graph_svg_export(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    db.connect().execute("INSERT INTO content_objects(hash_algorithm,full_hash,size_bytes) VALUES('sha256','a',1),('sha256','b',2)")
    db.connect().execute(
        "INSERT INTO content_relationships(source_type,source_id,target_type,target_id,relationship_type,evidence_tier,confidence,algorithm,algorithm_version) VALUES('CONTENT_OBJECT',1,'CONTENT_OBJECT',2,'PIXEL_IDENTICAL','TIER_2_NORMALIZED_EXACT',1.0,'x','1')"
    )
    db.connect().commit()
    projection = build_projection(db, "content-equivalence", max_nodes=10, max_edges=10)
    svg = to_svg(projection)
    assert svg.startswith("<svg")
    assert svg.count("<circle") == len(projection["nodes"])
    assert "</svg>" in svg
