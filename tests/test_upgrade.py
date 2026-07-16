from housekeeper.database import Database
from housekeeper.graph.builder import build_projection
from housekeeper.review.decisions import create_session, export_snapshot, record_decision


def test_content_identity_and_analysis_current(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    first = db.get_or_create_content_object("sha256", "a" * 64, 3)
    second = db.get_or_create_content_object("sha256", "a" * 64, 3)
    assert first == second
    assert not db.is_analysis_current(first, "documents", "1", "cfg")
    db.connect().execute(
        "INSERT INTO analysis_artifacts(content_object_id,analyzer_name,analyzer_version,configuration_fingerprint,status) VALUES(?,?,?,?,?)",
        (first, "documents", "1", "cfg", "COMPLETED"),
    )
    db.connect().commit()
    assert db.is_analysis_current(first, "documents", "1", "cfg")
    assert (
        db.fetch_one("SELECT status FROM migration_progress WHERE migration_version=4")["status"]
        == "COMPLETED"
    )


def test_review_history_snapshot_and_graph_limits(tmp_path):
    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    session = create_session(db, "test")
    decision = record_decision(db, session, "ENTRY", 1, "DEFER")
    record_decision(db, session, "ENTRY", 1, "MARK_KEEP")
    assert (
        db.fetch_one(
            "SELECT COUNT(*) AS n FROM review_decision_history WHERE decision_id=?", (decision,)
        )["n"]
        == 1
    )
    snapshot = export_snapshot(db, session)
    assert snapshot > 0
    db.connect().execute(
        "INSERT INTO relationships(source_type,source_id,target_type,target_id,relationship_type,confidence,relationship_version) VALUES('A',1,'B',2,'LINKS',0.9,'1')"
    )
    db.connect().commit()
    graph = build_projection(db, max_nodes=1, max_edges=1)
    assert len(graph["nodes"]) <= 1
    assert len(graph["edges"]) <= 1


def test_v1_database_backfills_verified_hashes(tmp_path):
    import sqlite3

    old = tmp_path / "old.sqlite"
    conn = sqlite3.connect(old)
    conn.executescript("""CREATE TABLE schema_migrations(version INTEGER PRIMARY KEY);
        INSERT INTO schema_migrations VALUES(1);
        CREATE TABLE scan_runs(id INTEGER PRIMARY KEY, source_root TEXT, source_root_fingerprint TEXT, status TEXT);
        CREATE TABLE filesystem_entries(id INTEGER PRIMARY KEY, scan_run_id INTEGER, entry_type TEXT, size_bytes INTEGER, relative_path TEXT, absolute_path TEXT, name TEXT, source_root TEXT);
        CREATE TABLE file_signatures(entry_id INTEGER PRIMARY KEY, full_hash TEXT, hash_algorithm TEXT, hash_status TEXT);
        INSERT INTO scan_runs VALUES(1,'/old','finger','COMPLETE');
        INSERT INTO filesystem_entries VALUES(1,1,'file',3,'a.txt','/old/a.txt','a.txt','/old');
        INSERT INTO file_signatures VALUES(1,'aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa','sha256','OK');""")
    conn.commit()
    conn.close()
    db = Database(old)
    db.initialize()
    assert db.database_stats()["schema_version"] == 4
    assert db.fetch_one("SELECT COUNT(*) AS n FROM content_objects")["n"] == 1
    assert db.fetch_one("SELECT COUNT(*) AS n FROM entry_content_links")["n"] == 1


def test_bounded_xlsx_extraction(tmp_path):
    import pytest

    pytest.importorskip("openpyxl")
    from openpyxl import Workbook
    from housekeeper.analyzers.documents import extract_document
    from housekeeper.config import load_config

    path = tmp_path / "sheet.xlsx"
    workbook = Workbook()
    workbook.active.append(["secret", 42])
    workbook.save(path)
    result = extract_document(path, ".xlsx", load_config())
    assert result["extraction_status"] == "OK"
    assert "secret" in result["normalized_text"]


def test_archive_path_traversal_is_error(tmp_path):
    import zipfile
    from housekeeper.analyzers.archives import inspect_archive
    from housekeeper.config import load_config

    path = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("../outside.txt", "unsafe")
    result = inspect_archive(path, load_config())
    assert result["analysis_status"] == "ERROR"


def test_incremental_scan_reuses_verified_content_and_full_analysis_hashes_inventory(tmp_path):
    from housekeeper.analyzers.registry import run_content_analysis
    from housekeeper.config import load_config
    from housekeeper.scanner import DriveScanner

    source = tmp_path / "source"
    source.mkdir()
    (source / "a.txt").write_text("same", encoding="utf-8")
    config = load_config(workspace_override=tmp_path / "workspace")
    db = Database(config.database_path)
    scanner = DriveScanner(db, config)
    scanner.scan(source, incremental=True)
    assert db.fetch_one("SELECT source_root_id FROM filesystem_entries WHERE entry_type='file'")[
        "source_root_id"
    ]
    run_content_analysis(db, config, "all")
    assert db.fetch_one("SELECT COUNT(*) AS n FROM content_objects")["n"] == 1
    scanner.scan(source, incremental=True, changed_only=True)
    assert (
        db.fetch_one(
            "SELECT COUNT(*) AS n FROM scan_entry_changes WHERE change_status='UNCHANGED'"
        )["n"]
        >= 1
    )


def test_duplicate_movement_refuses_last_verified_copy(tmp_path):
    import pytest
    from housekeeper.analyzers.exact_duplicates import run_exact_duplicate_analysis
    from housekeeper.config import load_config
    from housekeeper.models import ManifestEntry
    from housekeeper.review_mover import move_approved_entries
    from housekeeper.scanner import DriveScanner

    source = tmp_path / "source"
    source.mkdir()
    (source / "a.bin").write_bytes(b"duplicate")
    (source / "b.bin").write_bytes(b"duplicate")
    config = load_config(workspace_override=tmp_path / "workspace")
    db = Database(config.database_path)
    DriveScanner(db, config).scan(source)
    run_exact_duplicate_analysis(db, config)
    rows = db.fetch_all(
        "SELECT e.id,e.absolute_path,e.relative_path,e.size_bytes,s.full_hash FROM filesystem_entries e JOIN file_signatures s ON s.entry_id=e.id WHERE e.entry_type='file' ORDER BY e.id"
    )
    manifest = [
        ManifestEntry(
            True,
            row["id"],
            row["absolute_path"],
            row["relative_path"],
            row["size_bytes"],
            row["full_hash"],
            "REVIEW_SAFE",
            1.0,
            [],
            "",
            None,
            "",
        )
        for row in rows
    ]
    with pytest.raises(ValueError, match="last verified copy"):
        move_approved_entries(manifest, tmp_path / "review", db, dry_run=True, yes=False)


def test_dashboard_is_local_query_only_and_escaped(tmp_path):
    import pytest

    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from housekeeper.dashboard.app import create_app
    from fastapi.testclient import TestClient

    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    db.connect().execute(
        "INSERT INTO scan_runs(source_root,source_root_fingerprint,status) VALUES('/x','x','COMPLETE')"
    )
    db.connect().execute(
        "INSERT INTO filesystem_entries(scan_run_id,source_root,absolute_path,relative_path,name,entry_type) VALUES(1,'/x','/x/<script>','<script>','<script>','file')"
    )
    db.connect().commit()
    client = TestClient(create_app(db, read_only=True))
    assert client.get("/api/overview").status_code == 200
    assert "cytoscape.min.js" in client.get("/graph").text
    assert "htmx.min.js" in client.get("/").text
    assert client.get("/static/vendor/cytoscape.min.js").status_code == 200
    htmx = client.get("/static/vendor/htmx.min.js")
    assert htmx.status_code == 200
    assert "var htmx" in htmx.text
    assert client.get("/fragments/jobs").status_code == 200
    assert client.get("/fragments/entry/1").status_code == 200
    assert "&lt;script&gt;" in client.get("/review").text
    assert client.get("/api/graph/projection?projection_type=not-real").status_code == 422
    assert (
        client.post(
            "/api/review/decision?session_id=1&target_type=ENTRY&target_id=1&decision=MARK_KEEP"
        ).status_code
        == 403
    )


def test_dashboard_htmx_decision_form_requires_csrf_and_records(tmp_path):
    import pytest

    pytest.importorskip("fastapi")
    pytest.importorskip("httpx")
    from fastapi.testclient import TestClient
    from housekeeper.dashboard.app import create_app
    from housekeeper.review.decisions import create_session

    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    db.connect().execute(
        "INSERT INTO scan_runs(source_root,source_root_fingerprint,status) VALUES('/x','x','COMPLETE')"
    )
    db.connect().execute(
        "INSERT INTO filesystem_entries(scan_run_id,source_root,absolute_path,relative_path,name,entry_type,scan_status) VALUES(1,'/x','/x/a','a','a','file','OK')"
    )
    db.connect().commit()
    session_id = create_session(db, "dashboard")
    client = TestClient(create_app(db))
    form = {
        "entry_id": "1",
        "session_id": str(session_id),
        "decision": "MARK_KEEP",
        "note": "reviewed",
    }
    assert client.post("/fragments/review/decision", data=form).status_code == 403
    token = client.get("/api/csrf").json()["token"]
    response = client.post("/fragments/review/decision", data=form, headers={"X-CSRF-Token": token})
    assert response.status_code == 200
    assert db.fetch_one("SELECT user_note FROM review_decisions")["user_note"] == "reviewed"


def test_pause_is_durable_and_resume_returns_to_pending(tmp_path):
    import pytest

    from housekeeper.jobs import (
        JobPaused,
        check_cancelled,
        create_job,
        request_pause,
        resume_job,
        update_job,
    )

    db = Database(tmp_path / "db.sqlite")
    db.initialize()
    job_id = create_job(db, "CONTENT_ANALYSIS")
    update_job(db, job_id, "RUNNING")
    request_pause(db, job_id)
    with pytest.raises(JobPaused):
        check_cancelled(db, job_id)
    assert db.fetch_one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "PAUSED"
    resume_job(db, job_id)
    assert db.fetch_one("SELECT status FROM jobs WHERE id=?", (job_id,))["status"] == "PENDING"


def test_reappearing_file_has_historical_evidence(tmp_path):
    import json

    from housekeeper.config import load_config
    from housekeeper.scanner import DriveScanner

    source = tmp_path / "source"
    source.mkdir()
    target = source / "intermittent.txt"
    target.write_text("present", encoding="utf-8")
    config = load_config(workspace_override=tmp_path / "workspace")
    db = Database(config.database_path)
    scanner = DriveScanner(db, config)
    scanner.scan(source, incremental=True)
    target.unlink()
    scanner.scan(source, incremental=True)
    target.write_text("present", encoding="utf-8")
    scanner.scan(source, incremental=True)
    row = db.fetch_one(
        "SELECT evidence_json FROM scan_entry_changes WHERE scan_run_id=(SELECT MAX(id) FROM scan_runs) AND relative_path='intermittent.txt'"
    )
    assert row is not None
    assert json.loads(row["evidence_json"])["reappeared_after_missing"] is True


def test_source_association_survives_mount_change(tmp_path):
    from housekeeper.config import load_config
    from housekeeper.scanner import DriveScanner

    first_mount = tmp_path / "first-mount"
    second_mount = tmp_path / "second-mount"
    first_mount.mkdir()
    second_mount.mkdir()
    (first_mount / "same.txt").write_text("content", encoding="utf-8")
    (second_mount / "same.txt").write_text("content", encoding="utf-8")
    config = load_config(workspace_override=tmp_path / "workspace")
    db = Database(config.database_path)
    DriveScanner(db, config).scan(first_mount)
    source = db.fetch_one("SELECT id FROM source_roots")
    assert source is not None
    db.connect().execute(
        "UPDATE source_roots SET last_mount_path=? WHERE id=?", (str(second_mount), source["id"])
    )
    db.connect().commit()
    DriveScanner(db, config).scan(second_mount)
    assert db.fetch_one("SELECT COUNT(*) n FROM source_roots")["n"] == 1


def test_platform_identity_fallbacks(monkeypatch, tmp_path):
    import plistlib
    import subprocess

    import housekeeper.core.scan_identity as identity

    root = tmp_path / "source"
    root.mkdir()
    monkeypatch.setattr(identity.platform, "system", lambda: "Darwin")
    monkeypatch.setattr(
        identity.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args, 0, plistlib.dumps({"VolumeUUID": "mac-uuid", "VolumeName": "Drive"}), b""
        ),
    )
    assert identity.discover_source_identity(root)[:2] == ("mac-uuid", "Drive")
    monkeypatch.setattr(identity.platform, "system", lambda: "Linux")
    monkeypatch.setattr(
        identity.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args, 0, "linux-uuid Label ext4\n", ""),
    )
    assert identity.discover_source_identity(root)[:2] == ("linux-uuid", "Label")
    monkeypatch.setattr(identity.platform, "system", lambda: "Windows")
    uuid, _label, metadata = identity.discover_source_identity(root)
    assert uuid is None and "drive" in metadata


def test_unstable_full_hash_is_not_accepted(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from housekeeper.hashing import compute_full_hash

    target = tmp_path / "changing.bin"
    target.write_bytes(b"content")
    original_stat = target.stat
    states = [original_stat(), original_stat()]
    states[1] = SimpleNamespace(st_size=states[1].st_size + 1, st_mtime_ns=states[1].st_mtime_ns)
    monkeypatch.setattr("housekeeper.hashing.Path.stat", lambda _self: states.pop(0))
    result = compute_full_hash(target, "sha256", 1024)
    assert not result.stable
    assert result.digest is None
