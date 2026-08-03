"""In-browser folder picker: a read-only host directory browser behind the "Choose folder…" button.

A plain browser cannot open a native OS folder dialog for a server-side path, so the operational
dashboard serves /fragments/folders and a modal instead. It must list directories, navigate, stay
operational-only, and never crash on a bad path.
"""

import pytest

pytest.importorskip("fastapi")
pytest.importorskip("httpx")


@pytest.fixture
def client(config, database):
    from fastapi.testclient import TestClient

    from housekeeper.dashboard.app import create_app

    return TestClient(create_app(database, config=config))


def test_folders_fragment_lists_subdirectories(client, tmp_path):
    (tmp_path / "child_dir").mkdir()
    (tmp_path / "a_file.txt").write_text("x", encoding="utf-8")
    resp = client.get("/fragments/folders", params={"path": str(tmp_path)})
    assert resp.status_code == 200
    html = resp.text
    assert "child_dir" in html  # directories are listed
    assert "a_file.txt" not in html  # files are not
    assert "folder-use" in html and str(tmp_path) in html  # "Use this folder" carries the path
    assert "/fragments/folders?path=" in html  # navigable into the child


def test_folders_fragment_survives_a_bad_path(client):
    # A nonexistent path must fall back gracefully, never 500.
    resp = client.get("/fragments/folders", params={"path": "/no/such/place/at/all"})
    assert resp.status_code == 200


def test_folders_fragment_is_operational_only(database):
    # A viewer dashboard (no runner) must not expose a filesystem browser at all.
    from fastapi.testclient import TestClient

    from housekeeper.dashboard.app import create_app

    viewer = TestClient(create_app(database))
    assert viewer.get("/fragments/folders").status_code == 404


def test_scan_control_wires_the_picker(client):
    fragment = client.get("/fragments/control").text
    assert 'hx-get="/fragments/folders"' in fragment  # button opens the browser
    assert 'hx-preserve="true"' in fragment  # the 2s status poll no longer wipes the chosen path


def test_only_the_run_page_includes_the_folder_modal(client):
    assert "folder-browser-body" not in client.get("/activity").text
    assert "folder-browser-body" in client.get("/control").text
