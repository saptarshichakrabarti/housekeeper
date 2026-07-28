"""Image analyser tests: metadata, perceptual distance, decompression-bomb guard."""

import math

import pytest

from housekeeper.analysers.images import (
    DCT_SIZE,
    PHASH_BANDS,
    PHASH_BITS,
    SIMILARITY_THRESHOLD,
    dct_phash,
    extract_image_metadata,
    hash_distance,
    parse_phash,
    phash_bands,
)
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
    assert hash_distance(0b1010, 0b1010) == 0


def test_hash_distance_counts_differences():
    assert hash_distance(0b0000, 0b0011) == 2


def _band_offsets() -> list[int]:
    """The lowest bit index of each band."""
    offsets, position = [], 0
    for index in range(PHASH_BANDS):
        offsets.append(position)
        position += 8 if index == 0 else 7
    return offsets


def test_bands_are_complete_for_the_similarity_threshold():
    """The pigeonhole guarantee the candidate index rests on: no true match can be missed.

    Worst case for band coverage is spreading the flipped bits over as many distinct bands as
    possible. With 9 bands and a radius-8 threshold that always leaves one band untouched, so an
    equality join over bands finds every true match. The superseded 8-bit *prefix* bucket had no
    such property: a pair differing in one bit of the prefix was never compared at all.
    """
    base = 0x0F1E2D3C4B5A6978
    offsets = _band_offsets()
    assert len(offsets) == PHASH_BANDS
    assert len(phash_bands(base)) == PHASH_BANDS
    for skipped in range(PHASH_BANDS):  # every choice of 8 bands out of 9
        other = base
        for index, offset in enumerate(offsets):
            if index != skipped:
                other ^= 1 << offset
        assert hash_distance(base, other) == SIMILARITY_THRESHOLD
        matching = [a == b for a, b in zip(phash_bands(base), phash_bands(other))]
        assert matching[skipped] and sum(matching) == 1


def test_nine_scattered_bits_are_beyond_the_threshold():
    """The boundary is not arbitrary: at distance 9 no band need match, and none is claimed to."""
    base = 0x0F1E2D3C4B5A6978
    other = base
    for offset in _band_offsets():
        other ^= 1 << offset
    assert hash_distance(base, other) == PHASH_BANDS > SIMILARITY_THRESHOLD
    assert not any(a == b for a, b in zip(phash_bands(base), phash_bands(other)))



def test_parse_phash_rejects_the_superseded_bit_string():
    """A 64-character bit string is valid hex and would silently parse into a nonsense number."""
    assert parse_phash("1" * 64) is None
    assert parse_phash("00000000000000ff") == 255
    assert parse_phash(None) is None


def test_extract_metadata(tmp_path, cfg):
    path = _image(tmp_path / "a.png", (10, 20, 30))
    result = extract_image_metadata(path, cfg)
    assert result["analysis_status"] == "OK"
    assert result["width"] == 32 and result["height"] == 32
    assert parse_phash(result["perceptual_hash"]) is not None
    assert "capture_time" in result  # read at parse time; clustering never re-opens the file


def test_similar_images_have_small_distance(tmp_path, cfg):
    original = extract_image_metadata(_image(tmp_path / "a.png", (10, 120, 200), (64, 64)), cfg)
    resized = extract_image_metadata(_image(tmp_path / "b.png", (10, 120, 200), (32, 32)), cfg)
    distance = hash_distance(
        parse_phash(original["perceptual_hash"]), parse_phash(resized["perceptual_hash"])
    )
    assert distance <= SIMILARITY_THRESHOLD


def test_decompression_bomb_guard(tmp_path, cfg):
    path = _image(tmp_path / "big.png", (0, 0, 0), (64, 64))
    cfg.section("images")["max_pixels"] = 16  # far below 64*64
    result = extract_image_metadata(path, cfg)
    assert result["analysis_status"] != "OK"


# --- DCT descriptor validation corpus ------------------------------------------------------------
# The descriptor change was held back because "a different descriptor relates a different set of
# images", and that is not something a speed measurement can settle. What settles it is a corpus of
# transformations with a stated expectation for each: re-encoding, mild exposure and scale changes
# must *not* move an image out of its own cluster; a different picture must not fall into it.


def _photo(path, seed: int, size=(160, 120), gamma: float = 1.0, quality: int = 95):
    """A deterministic synthetic photograph: smooth gradients plus seed-dependent structure.

    Flat colour will not do — every descriptor agrees on a flat field. This has low-frequency
    content for the DCT to find and enough structure that two seeds are genuinely different images.
    """
    from PIL import Image

    width, height = size
    image = Image.new("L", size)
    pixels = []
    # Frequencies must be distinct *per seed*, not seed modulo a small number: with `seed % 3` /
    # `seed % 5`, seeds 0 and 6 differed only in one low-frequency term, so they were near-duplicate
    # pictures and any honest descriptor called them similar. That was a corpus bug masquerading as
    # a false positive.
    x_frequency, y_frequency, diagonal = 1 + seed, 2 + 2 * seed, 3 + 5 * seed
    for y in range(height):
        for x in range(width):
            value = (
                90
                + 60 * math.sin((x / width) * math.pi * x_frequency)
                + 40 * math.cos((y / height) * math.pi * y_frequency)
                + 25 * math.sin(((x + y) / (width + height)) * math.pi * diagonal)
            )
            pixels.append(max(0, min(255, int(value))))
    image.putdata(pixels)
    if gamma != 1.0:
        table = [max(0, min(255, int(255 * ((i / 255) ** gamma)))) for i in range(256)]
        image = image.point(table)
    image.convert("RGB").save(path, "JPEG", quality=quality)
    return path


def _descriptor(path, cfg) -> int:
    result = extract_image_metadata(path, cfg)
    assert result["analysis_status"] == "OK", result.get("analysis_error")
    value = parse_phash(result["perceptual_hash"])
    assert value is not None, result["perceptual_hash"]
    return value


#: (name, transform kwargs, must stay within the similarity threshold)
TRANSFORMS = [
    ("identical", {}, True),
    ("jpeg_quality_60", {"quality": 60}, True),
    ("jpeg_quality_30", {"quality": 30}, True),
    # Gamma both ways, and hard enough to matter: 0.7 is a visibly brighter image. Before the rank
    # transform these cost 20 and 16 bits of a 64-bit descriptor against a threshold of 8.
    ("gamma_0_7", {"gamma": 0.7}, True),
    ("gamma_0_8", {"gamma": 0.8}, True),
    ("gamma_1_3", {"gamma": 1.3}, True),
    ("gamma_1_6", {"gamma": 1.6}, True),
    ("scaled_down", {"size": (80, 60)}, True),
    ("scaled_up", {"size": (320, 240)}, True),
]


@pytest.mark.parametrize("name,kwargs,should_match", TRANSFORMS, ids=[t[0] for t in TRANSFORMS])
def test_descriptor_survives_benign_transformations(tmp_path, cfg, name, kwargs, should_match):
    original = _descriptor(_photo(tmp_path / "base.jpg", seed=1), cfg)
    variant = _descriptor(_photo(tmp_path / f"{name}.jpg", seed=1, **kwargs), cfg)
    distance = hash_distance(original, variant)
    assert (distance <= SIMILARITY_THRESHOLD) is should_match, (
        f"{name}: distance {distance} against a threshold of {SIMILARITY_THRESHOLD}"
    )


def test_different_photographs_are_not_similar(tmp_path, cfg):
    """The half that matters for a review tool: no false 'these are the same picture'."""
    descriptors = [
        _descriptor(_photo(tmp_path / f"seed{seed}.jpg", seed=seed), cfg) for seed in range(8)
    ]
    distances = [
        hash_distance(descriptors[a], descriptors[b])
        for a in range(len(descriptors))
        for b in range(a + 1, len(descriptors))
    ]
    closest = min(distances)
    assert closest > SIMILARITY_THRESHOLD, (
        f"distinct images called similar: closest pair {closest} bits apart, "
        f"threshold {SIMILARITY_THRESHOLD}"
    )
    # Margin, not just correctness. Before the rank transform the closest distinct pair sat at
    # exactly 8 — technically passing, one JPEG artefact away from a false positive. A descriptor
    # change that erodes separation back to the threshold should fail here rather than in the field.
    assert closest >= 2 * SIMILARITY_THRESHOLD, (
        f"separation margin has eroded: closest distinct pair {closest} bits, "
        f"want at least {2 * SIMILARITY_THRESHOLD}"
    )


def test_dct_descriptor_is_reproducible_and_bounded():
    """Same samples, same descriptor — it is persisted and compared across runs and machines."""
    samples = bytes((x * 7 + y * 13) % 256 for y in range(DCT_SIZE) for x in range(DCT_SIZE))
    first, second = dct_phash(samples), dct_phash(samples)
    assert first == second
    assert 0 <= first < 1 << PHASH_BITS


def test_dct_descriptor_rejects_the_wrong_sample_count():
    with pytest.raises(ValueError, match="grayscale samples"):
        dct_phash(b"\x00" * 100)


def test_dct_descriptor_ignores_a_uniform_brightness_shift():
    """DC is excluded from the threshold, so exposure alone must not change a single bit.

    This is the concrete defect the average hash had: it thresholded at the mean, so brightening an
    image moved pixels across the threshold and the descriptor drifted.
    """
    base = [(x * 3 + y * 5) % 200 for y in range(DCT_SIZE) for x in range(DCT_SIZE)]
    brighter = bytes(min(255, value + 40) for value in base)
    assert dct_phash(bytes(base)) == dct_phash(brighter)
