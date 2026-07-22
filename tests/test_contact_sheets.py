"""Contact-sheet (montage) generation for image-similarity groups.

Contact sheets composite existing thumbnails of an IMAGE_SIMILARITY group into one bounded grid.
These tests build a real group (visually similar but byte-distinct images), then assert the sheet
is produced, is a valid deterministic JPEG, and degrades safely when disabled or thumbnail-less.
"""

import shutil

import pytest

pytest.importorskip("PIL")

from housekeeper.analyzers.contact_sheets import (  # noqa: E402
    contact_sheet_path,
    run_contact_sheet_generation,
)
from housekeeper.analyzers.images import run_image_analysis  # noqa: E402
from housekeeper.analyzers.registry import run_content_analysis  # noqa: E402
from housekeeper.scanner import DriveScanner  # noqa: E402


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
