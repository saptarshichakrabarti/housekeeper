"""Image analyser tests: metadata, perceptual distance, decompression-bomb guard."""

import pytest

from housekeeper.analysers.images import calculate_hash_distance, extract_image_metadata
from housekeeper.config import load_config

pytest.importorskip("PIL")


@pytest.fixture
def cfg():
    return load_config()


def _image(path, color, size=(32, 32)):
    from PIL import Image

    Image.new("RGB", size, color).save(path)
    return path


def test_hash_distance_zero_for_equal():
    assert calculate_hash_distance("1010", "1010") == 0


def test_hash_distance_counts_differences():
    assert calculate_hash_distance("0000", "0011") == 2


def test_extract_metadata(tmp_path, cfg):
    path = _image(tmp_path / "a.png", (10, 20, 30))
    result = extract_image_metadata(path, cfg)
    assert result["analysis_status"] == "OK"
    assert result["width"] == 32 and result["height"] == 32
    assert result["perceptual_hash"]


def test_similar_images_have_small_distance(tmp_path, cfg):
    original = extract_image_metadata(_image(tmp_path / "a.png", (10, 120, 200), (64, 64)), cfg)
    resized = extract_image_metadata(_image(tmp_path / "b.png", (10, 120, 200), (32, 32)), cfg)
    distance = calculate_hash_distance(original["perceptual_hash"], resized["perceptual_hash"])
    assert distance <= 8


def test_decompression_bomb_guard(tmp_path, cfg):
    path = _image(tmp_path / "big.png", (0, 0, 0), (64, 64))
    cfg.section("images")["max_pixels"] = 16  # far below 64*64
    result = extract_image_metadata(path, cfg)
    assert result["analysis_status"] != "OK"
