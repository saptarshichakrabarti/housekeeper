"""Dashboard detail workflows: side-by-side backup compare, derivation timeline, image-group
detail with contact sheet. All GET-only, bounded, escaped; the pages present facts and existing
review affordances — they never move data."""

import pytest

from housekeeper.database import Database

pytest.importorskip("fastapi")
pytest.importorskip("httpx")


def _seed(db):
    conn = db.connect()
    conn.execute(
        "INSERT INTO scan_runs(source_root,source_root_fingerprint,status) VALUES('/x','x','COMPLETE')"
    )
    # Two directories with summaries and a backup relationship between them.
    conn.execute(
        "INSERT INTO filesystem_entries(id,scan_run_id,source_root_id,source_root,entry_type,name,relative_path,absolute_path,modified_at) VALUES"
        "(1,1,NULL,'/x','directory','Original','Original','/x/Original',NULL),"
        "(2,1,NULL,'/x','directory','Backup','Backup','/x/Backup',NULL),"
        "(3,1,NULL,'/x','file','draft.docx','Original/draft.docx','/x/Original/draft.docx',1000.0),"
        "(4,1,NULL,'/x','file','draft.pdf','Original/draft.pdf','/x/Original/draft.pdf',1120.0)"
    )
    conn.execute(
        "INSERT INTO directory_summaries(entry_id,recursive_file_count,recursive_directory_count,recursive_size_bytes,unique_full_hash_count,duplicate_file_count) VALUES"
        "(1,10,2,4096,9,1),(2,8,2,3072,8,0)"
    )
    conn.execute(
        "INSERT INTO relationships(id,source_type,source_id,target_type,target_id,relationship_type,confidence,evidence_json,relationship_version) VALUES"
        "(1,'DIRECTORY',1,'DIRECTORY',2,'LIKELY_BACKUP_SUCCESSOR',0.9,'{\"shared_hashes\": 7}','1'),"
        "(2,'ENTRY',3,'ENTRY',4,'LIKELY_VERSION_OF',0.9,'{}','1')"
    )
    # Content objects, links, and a derivation relationship for the timeline.
    conn.execute(
        "INSERT INTO content_objects(id,hash_algorithm,full_hash,size_bytes) VALUES"
        "(1,'sha256','a',10),(2,'sha256','b',20),(3,'sha256','c',30)"
    )
    conn.execute(
        "INSERT INTO entry_content_links(entry_id,content_object_id,link_status) VALUES"
        "(3,1,'VERIFIED'),(4,2,'VERIFIED')"
    )
    conn.execute(
        "INSERT INTO content_relationships(source_type,source_id,target_type,target_id,relationship_type,evidence_tier,confidence,algorithm,algorithm_version,evidence_json,explanation) VALUES"
        "('CONTENT_OBJECT',1,'CONTENT_OBJECT',2,'LIKELY_EXPORT','TIER_6_CONTEXTUAL_INFERENCE',0.85,'x','1','{\"modified_gap_seconds\": 120}','docx exported to pdf')"
    )
    # An image-similarity group over content objects 1 and 3.
    conn.execute(
        "INSERT INTO relationship_groups(id,group_type,group_key,relationship_version) VALUES"
        "(1,'IMAGE_SIMILARITY','abcd1234','2')"
    )
    conn.execute(
        "INSERT INTO relationship_group_members(group_id,content_object_id) VALUES(1,1),(1,3)"
    )
    conn.commit()


@pytest.fixture
def sheet_dir(tmp_path):
    directory = tmp_path / "sheets"
    directory.mkdir()
    return directory


@pytest.fixture
def client(tmp_path, sheet_dir):
    from fastapi.testclient import TestClient

    from housekeeper.dashboard.app import create_app

    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    _seed(db)
    return TestClient(create_app(db, contact_sheet_dir=sheet_dir))


def test_backup_explorer_links_to_compare(client):
    response = client.get("/backups")
    assert response.status_code == 200
    assert "/backups/1" in response.text


def test_backup_compare_side_by_side(client):
    response = client.get("/backups/1")
    assert response.status_code == 200
    body = response.text
    assert "Original" in body and "Backup" in body  # both directories present
    assert "LIKELY_BACKUP_SUCCESSOR" in body
    assert "shared_hashes" in body  # evidence surfaced
    assert "10" in body and "8" in body  # recursive file counts side by side


def test_backup_compare_rejects_non_directory_relationship(client):
    assert client.get("/backups/2").status_code == 404  # ENTRY-to-ENTRY relationship
    assert client.get("/backups/999").status_code == 404


def test_derivation_timeline(client):
    response = client.get("/derivations/1")
    assert response.status_code == 200
    body = response.text
    assert "draft.docx" in body and "draft.pdf" in body
    assert "LIKELY_EXPORT" in body
    assert "120" in body  # modified gap surfaced
    assert client.get("/derivations/999").status_code == 404


def test_image_group_detail_without_sheet(client):
    response = client.get("/images/1")
    assert response.status_code == 200
    assert "abcd1234" in response.text
    assert "analyze contact-sheets" in response.text  # hint when no sheet rendered
    assert client.get("/images/999").status_code == 404
    assert client.get("/contact-sheets/1.jpg").status_code == 404


def test_image_group_detail_with_sheet(client, sheet_dir):
    pytest.importorskip("PIL")
    from PIL import Image

    Image.new("RGB", (32, 32), (40, 80, 120)).save(sheet_dir / "group_1.jpg", "JPEG")
    detail = client.get("/images/1")
    assert "/contact-sheets/1.jpg" in detail.text
    sheet = client.get("/contact-sheets/1.jpg")
    assert sheet.status_code == 200
    assert sheet.headers["content-type"] == "image/jpeg"
    assert sheet.content[:2] == b"\xff\xd8"  # JPEG magic


def test_images_explorer_lists_groups(client):
    response = client.get("/images")
    assert response.status_code == 200
    assert "Similarity groups" in response.text
    assert "/images/1" in response.text


def test_detail_pages_are_readable_in_read_only_mode(tmp_path, sheet_dir):
    from fastapi.testclient import TestClient

    from housekeeper.dashboard.app import create_app

    db = Database(tmp_path / "ro.sqlite")
    db.initialize()
    _seed(db)
    read_only = TestClient(create_app(db, read_only=True, contact_sheet_dir=sheet_dir))
    # Detail views are GET-only facts; read-only mode must not hide them.
    assert read_only.get("/backups/1").status_code == 200
    assert read_only.get("/derivations/1").status_code == 200
    assert read_only.get("/images/1").status_code == 200
