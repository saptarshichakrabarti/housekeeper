"""Format-aware normalized-equivalence tests (Tier-2/3) and their safety invariants."""

import zipfile

import pytest

from housekeeper.analyzers.normalized_content import run_normalized_content_analysis
from housekeeper.normalization.archives import normalize_archive_content
from housekeeper.normalization.office_xml import normalize_office
from housekeeper.normalization.registry import IMAGE_PIXEL_PROFILE, get_or_create_profile_id
from housekeeper.scanner import DriveScanner


def _relationships(database, relationship_type=None):
    if relationship_type:
        return database.fetch_all(
            "SELECT * FROM content_relationships WHERE relationship_type=? AND status='ACTIVE'",
            (relationship_type,),
        )
    return database.fetch_all("SELECT * FROM content_relationships WHERE status='ACTIVE'")


def _make_docx(path, member_order_reversed=False, text="hello", deflate=False):
    members = [
        ("word/document.xml", f"<w:document>{text}</w:document>"),
        ("[Content_Types].xml", "<Types/>"),
        ("docProps/core.xml", "<core><modified>2020</modified></core>"),
    ]
    if member_order_reversed:
        members = list(reversed(members))
    compression = zipfile.ZIP_DEFLATED if deflate else zipfile.ZIP_STORED
    with zipfile.ZipFile(path, "w", compression) as archive:
        for name, body in members:
            archive.writestr(name, body)


def _image(path, color, size=(40, 40)):
    from PIL import Image

    image = Image.new("RGB", size, color)
    for x in range(min(size)):
        image.putpixel((x, x), (200, 10, 10))
    image.save(path)
    return path


# --- Images -----------------------------------------------------------------------------

def test_pixel_identical_is_tier2_not_exact(config, database, tmp_path):
    pytest.importorskip("PIL")
    root = tmp_path / "src"
    root.mkdir()
    _image(root / "photo.png", (12, 34, 56))
    _image(root / "photo.bmp", (12, 34, 56))  # same pixels, different container bytes
    DriveScanner(database, config).scan(root, incremental=False)
    run_normalized_content_analysis(database, config)
    pixel = _relationships(database, "PIXEL_IDENTICAL")
    assert len(pixel) == 1
    assert pixel[0]["evidence_tier"] == "TIER_2_NORMALIZED_EXACT"
    # Different bytes -> must NOT be an exact (byte-identical) duplicate group.
    assert database.fetch_one("SELECT COUNT(*) n FROM exact_duplicate_groups")["n"] == 0


def test_resized_image_is_not_pixel_identical(config, database, tmp_path):
    pytest.importorskip("PIL")
    root = tmp_path / "src"
    root.mkdir()
    _image(root / "big.png", (10, 20, 30), (64, 64))
    _image(root / "small.png", (10, 20, 30), (32, 32))
    DriveScanner(database, config).scan(root, incremental=False)
    run_normalized_content_analysis(database, config)
    assert _relationships(database, "PIXEL_IDENTICAL") == []


# --- Office -----------------------------------------------------------------------------

def test_office_repackaging_is_equivalent(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    _make_docx(root / "a.docx", member_order_reversed=False, deflate=False)
    _make_docx(root / "b.docx", member_order_reversed=True, deflate=True)  # same content, repackaged
    DriveScanner(database, config).scan(root, incremental=False)
    run_normalized_content_analysis(database, config)
    equivalent = _relationships(database, "OFFICE_PACKAGE_EQUIVALENT")
    assert len(equivalent) == 1
    assert equivalent[0]["evidence_tier"] == "TIER_2_NORMALIZED_EXACT"


def test_office_with_different_text_is_not_equivalent(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    _make_docx(root / "a.docx", text="first draft")
    _make_docx(root / "b.docx", text="a meaningfully different body")
    DriveScanner(database, config).scan(root, incremental=False)
    run_normalized_content_analysis(database, config)
    assert _relationships(database, "OFFICE_PACKAGE_EQUIVALENT") == []


def test_office_normalization_ignores_only_volatile_properties(config, database, tmp_path):
    a = tmp_path / "a.docx"
    b = tmp_path / "b.docx"
    _make_docx(a, deflate=False)
    _make_docx(b, member_order_reversed=True, deflate=True)  # only ordering/compression differ
    first = normalize_office(a, config)
    second = normalize_office(b, config)
    assert first.normalized_hash == second.normalized_hash


# --- Archives ---------------------------------------------------------------------------

def test_archive_repackaging_variant(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    for name, compression in (("one.zip", zipfile.ZIP_STORED), ("two.zip", zipfile.ZIP_DEFLATED)):
        with zipfile.ZipFile(root / name, "w", compression) as archive:
            archive.writestr("dir/a.txt", "shared body a")
            archive.writestr("dir/b.txt", "shared body b")
    DriveScanner(database, config).scan(root, incremental=False)
    run_normalized_content_analysis(database, config)
    variants = _relationships(database, "ARCHIVE_REPACKAGING_VARIANT")
    assert len(variants) == 1


def test_archive_with_different_members_not_equivalent(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    with zipfile.ZipFile(root / "one.zip", "w") as archive:
        archive.writestr("a.txt", "alpha")
    with zipfile.ZipFile(root / "two.zip", "w") as archive:
        archive.writestr("a.txt", "beta")
    DriveScanner(database, config).scan(root, incremental=False)
    run_normalized_content_analysis(database, config)
    assert _relationships(database, "ARCHIVE_REPACKAGING_VARIANT") == []


def test_oversized_archive_reports_unsupported(config, tmp_path):
    path = tmp_path / "big.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("a.txt", "x")
    config.data["normalization"]["archives"]["max_content_bytes"] = 0
    assert normalize_archive_content(path, config).status == "UNSUPPORTED"


# --- Determinism / provenance -----------------------------------------------------------

def test_normalization_is_deterministic_and_records_loss(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    _image(root / "photo.png", (5, 5, 5))
    DriveScanner(database, config).scan(root, incremental=False)
    run_normalized_content_analysis(database, config)
    profile_id = get_or_create_profile_id(database, IMAGE_PIXEL_PROFILE)
    first = database.fetch_one(
        "SELECT normalized_hash FROM normalized_content_artifacts WHERE normalization_profile_id=?",
        (profile_id,),
    )["normalized_hash"]
    run_normalized_content_analysis(database, config)
    second = database.fetch_one(
        "SELECT normalized_hash FROM normalized_content_artifacts WHERE normalization_profile_id=?",
        (profile_id,),
    )["normalized_hash"]
    assert first == second
    loss = database.fetch_one(
        "SELECT loss_characteristics_json FROM normalization_profiles WHERE id=?", (profile_id,)
    )["loss_characteristics_json"]
    assert "exif" in loss


def test_representative_path_fallback(config, database, tmp_path):
    """When two paths share one content object, a missing first path falls back to the second."""
    import shutil

    root = tmp_path / "src"
    (root / "sub").mkdir(parents=True)
    _make_docx(root / "a.docx")
    shutil.copy2(root / "a.docx", root / "sub" / "a-copy.docx")  # byte-identical: one content object
    DriveScanner(database, config).scan(root, incremental=False)
    from housekeeper.analyzers.exact_duplicates import run_exact_duplicate_analysis

    run_exact_duplicate_analysis(database, config)  # link both paths to one content object
    (root / "a.docx").unlink()  # remove the first representative
    result = run_normalized_content_analysis(database, config)
    assert result["errors"] == 0
    assert result["normalized"] >= 1  # normalized via the surviving path
