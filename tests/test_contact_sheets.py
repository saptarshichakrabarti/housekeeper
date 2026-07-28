"""Contact-sheet (montage) generation for image-similarity groups.

Contact sheets composite existing thumbnails of an IMAGE_SIMILARITY group into one bounded grid.
These tests build a real group (visually similar but byte-distinct images), then assert the sheet
is produced, is a valid deterministic JPEG, and degrades safely when disabled or thumbnail-less.
"""

import shutil

import pytest

pytest.importorskip("PIL")

from housekeeper.analysers.contact_sheets import (
    contact_sheet_path,
    run_contact_sheet_generation,
)
from housekeeper.analysers.images import run_image_analysis
from housekeeper.analysers.registry import run_content_analysis
from housekeeper.scanner import DriveScanner


def _solid(path, color, size=(48, 48)):
    from PIL import Image

    Image.new("RGB", size, color).save(path)


def _prepare_group(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    # Solid colours all hash to the same perceptual value (identical average) so they group, yet the
    # distinct colours make them byte-distinct -> three separate content objects in one group.
    _solid(root / "a.png", (10, 120, 200))
    _solid(root / "b.png", (12, 122, 202))
    _solid(root / "c.png", (14, 124, 204))
    DriveScanner(database, config).scan(root, incremental=False)
    run_content_analysis(database, config, "images")  # image metadata + thumbnails
    run_image_analysis(database, config)  # IMAGE_SIMILARITY groups
    return database.fetch_all(
        "SELECT id FROM relationship_groups WHERE group_type='IMAGE_SIMILARITY'"
    )


def test_contact_sheet_written_for_group(config, database, tmp_path):
    groups = _prepare_group(config, database, tmp_path)
    assert groups, "expected an IMAGE_SIMILARITY group"
    result = run_contact_sheet_generation(database, config)
    assert result["status"] == "ok"
    assert result["sheets_written"] >= 1

    from PIL import Image

    for group in groups:
        path = contact_sheet_path(config, int(group["id"]))
        assert path.is_file()
        with Image.open(path) as sheet:
            assert sheet.format == "JPEG"
            assert sheet.width > 0 and sheet.height > 0


def test_contact_sheet_is_deterministic(config, database, tmp_path):
    groups = _prepare_group(config, database, tmp_path)
    group_id = int(groups[0]["id"])
    run_contact_sheet_generation(database, config)
    first = contact_sheet_path(config, group_id).read_bytes()
    run_contact_sheet_generation(database, config)
    second = contact_sheet_path(config, group_id).read_bytes()
    assert first == second  # sorted members, fixed layout, no timestamps embedded


def test_contact_sheet_respects_disable_flag(config, database, tmp_path):
    _prepare_group(config, database, tmp_path)
    config.section("images")["create_contact_sheets"] = False
    result = run_contact_sheet_generation(database, config)
    assert result["status"] == "disabled"
    assert result["sheets_written"] == 0


def test_contact_sheet_skips_group_without_thumbnails(config, database, tmp_path):
    _prepare_group(config, database, tmp_path)
    thumb_dir = config.workspace / ".housekeeper" / "thumbnails"
    if thumb_dir.exists():
        shutil.rmtree(thumb_dir)  # no member now has a usable thumbnail
    result = run_contact_sheet_generation(database, config)
    assert result["sheets_written"] == 0
    assert result["skipped_groups"] >= 1


def test_reuse_key_is_cascaded_when_its_group_is_replaced(config, database, tmp_path):
    """The reason the key is a row and not a sidecar beside the sheet.

    `replace_relationship_group` deletes and reinserts groups, so a sidecar file could outlive the
    group it described and authorise reusing a sheet rendered for an entirely different set of
    members. A cascaded row cannot.
    """
    from housekeeper.relationships import replace_relationship_group

    conn = database.connect()
    conn.execute(
        "INSERT INTO content_objects(id,hash_algorithm,full_hash,size_bytes) VALUES"
        "(1,'sha256','a',1),(2,'sha256','b',2),(3,'sha256','c',3)"
    )
    group_id = replace_relationship_group(database, "IMAGE_SIMILARITY", "k" * 16, [1, 2], {}, "3")
    conn.execute(
        "INSERT INTO contact_sheet_renders(group_id,input_key) VALUES(?, 'stale-key')", (group_id,)
    )
    conn.commit()
    assert database.fetch_one("SELECT COUNT(*) n FROM contact_sheet_renders")["n"] == 1

    # Same key, different membership: the group row is replaced.
    replace_relationship_group(database, "IMAGE_SIMILARITY", "k" * 16, [1, 3], {}, "3")
    database.connect().commit()
    surviving = database.fetch_one(
        "SELECT COUNT(*) n FROM contact_sheet_renders r "
        "WHERE NOT EXISTS(SELECT 1 FROM relationship_groups g WHERE g.id=r.group_id)"
    )["n"]
    assert surviving == 0, "a reuse key outlived its group"
