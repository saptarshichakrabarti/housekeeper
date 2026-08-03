"""Phase 5: the analysers that used to redo their whole corpus on every run.

Each test runs a stage twice over an unchanged inventory and asserts the second run does no work
of the expensive kind — parsing, re-normalising, re-streaming an archive, re-rendering a montage.
The counts, not the wall clock, are what is asserted, so these hold in CI.
"""

from __future__ import annotations

import json
import zipfile

import pytest

from housekeeper.analysers import archive_equivalence
from housekeeper.analysers.archive_equivalence import run_archive_directory_analysis
from housekeeper.analysers.images import PHASH_BANDS, refresh_phash_index, run_image_analysis
from housekeeper.analysers.normalized_content import run_normalized_content_analysis
from housekeeper.analysers.registry import run_content_analysis
from housekeeper.analysers.scope import resolve_scope
from housekeeper.scanner import DriveScanner


def _scan(config, database, root):
    DriveScanner(database, config).scan(root, incremental=False)


def test_normalisation_skips_objects_that_already_have_an_artifact(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    for index in range(3):
        (root / f"table{index}.csv").write_text(f"a,b\n{index},2\n")
    _scan(config, database, root)

    first = run_normalized_content_analysis(database, config)
    assert first["normalized"] >= 3, "expected the first run to normalise the corpus"
    second = run_normalized_content_analysis(database, config)
    # The schema has always had UNIQUE(content_object_id, normalization_profile_id); the anti-join
    # is what finally asks it whether the work is already done.
    assert second["normalized"] == 0
    assert second["errors"] == 0 and second["unsupported"] == 0
    # Reuse must not cost the relationships: they are still emitted from the stored artifacts.
    assert second["relationships"] == first["relationships"]


def test_archive_members_are_streamed_once_per_content_object(config, database, tmp_path, monkeypatch):
    root = tmp_path / "src"
    (root / "tree").mkdir(parents=True)
    for index in range(3):
        (root / "tree" / f"f{index}.txt").write_text(f"payload {index}")
    for copy in ("one.zip", "two.zip"):  # byte-identical copies => one content object
        with zipfile.ZipFile(root / copy, "w") as archive:
            for index in range(3):
                archive.writestr(f"f{index}.txt", f"payload {index}")
    _scan(config, database, root)

    streamed: list[str] = []
    original = archive_equivalence._member_hashes

    def counting(path, max_members, algorithm):
        streamed.append(str(path))
        return original(path, max_members, algorithm)

    monkeypatch.setattr(archive_equivalence, "_member_hashes", counting)

    run_archive_directory_analysis(database, config)
    assert len(streamed) == 1, f"two identical copies should stream once, streamed {streamed}"
    streamed.clear()
    run_archive_directory_analysis(database, config)
    assert streamed == [], "a rescan of an unchanged archive must not re-stream it"


def test_phash_index_is_rebuilt_only_for_changed_descriptors(config, database, tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    root = tmp_path / "src"
    root.mkdir()
    for index, colour in enumerate([(10, 120, 200), (12, 122, 202), (200, 30, 30)]):
        Image.new("RGB", (48, 48), colour).save(root / f"i{index}.png")
    _scan(config, database, root)
    run_content_analysis(database, config, "images")

    scope = resolve_scope(database, None)
    assert refresh_phash_index(database, scope) == 3
    assert refresh_phash_index(database, scope) == 0, "unchanged descriptors must reindex nothing"
    rows = database.fetch_one("SELECT COUNT(*) AS n FROM image_phash_bands")
    assert rows["n"] == 3 * PHASH_BANDS


def test_similarity_survives_a_single_flipped_bit_in_the_old_prefix(config, database, tmp_path):
    """The false negative the prefix bucket produced, as a database-level regression test.

    Two descriptors differing only in the top bit landed in different 8-bit prefix buckets and were
    never compared, despite being at distance 1. The band index puts them in the same candidate set.
    """
    ids = _images_with_descriptors(
        config, database, tmp_path, ["7fffffffffffffff", "ffffffffffffffff"]
    )
    run_image_analysis(database, config)
    pairs = database.fetch_all(
        "SELECT source_id,target_id FROM relationships WHERE relationship_type='VISUALLY_SIMILAR_TO'"
    )
    assert [(row["source_id"], row["target_id"]) for row in pairs] == [(ids[0], ids[1])]
    groups = database.fetch_all(
        "SELECT group_key FROM relationship_groups WHERE group_type='IMAGE_SIMILARITY'"
    )
    # Keyed by the lowest member's descriptor, so the key is content-derived and stable.
    assert [row["group_key"] for row in groups] == ["7fffffffffffffff"]


def test_distant_descriptors_are_candidates_but_never_related(config, database, tmp_path):
    """G3: sharing a band is a candidate, not a verdict. The exact distance still gates."""
    _images_with_descriptors(
        config, database, tmp_path, ["00000000000000ff", "0000ffffffff00ff"]
    )  # equal low band -> candidates; distance 32 -> no relationship
    run_image_analysis(database, config)
    assert database.fetch_all(
        "SELECT 1 FROM relationships WHERE relationship_type='VISUALLY_SIMILAR_TO'"
    ) == []
    assert database.fetch_all(
        "SELECT 1 FROM relationship_groups WHERE group_type='IMAGE_SIMILARITY'"
    ) == []


def test_clustering_reads_capture_time_from_the_artifact(config, database, tmp_path):
    """EXIF is read once at parse time, not by re-opening every photograph on every run.

    The files' modification times are seconds apart, so on mtime alone they are one event. The
    stored capture times are days apart, so if the artifact is being read they are not.
    """
    from housekeeper.collections.events import run_photo_event_analysis

    root = tmp_path / "src"
    root.mkdir()
    for index in range(4):
        (root / f"img{index}.png").write_bytes(f"pretend png {index}".encode())
    _scan(config, database, root)

    connection = database.connect()
    base = 1_700_000_000.0
    for index, row in enumerate(
        database.fetch_all(
            "SELECT id,name FROM filesystem_entries WHERE entry_type='file' ORDER BY name"
        )
    ):
        content_id = index + 1
        connection.execute(
            "INSERT INTO content_objects(id,hash_algorithm,full_hash,size_bytes) VALUES(?,?,?,?)",
            (content_id, "sha256", f"{content_id:064x}", 1),
        )
        connection.execute(
            "INSERT INTO entry_content_links(entry_id,content_object_id,link_status) VALUES(?,?,?)",
            (int(row["id"]), content_id, "VERIFIED"),
        )
        connection.execute(
            """INSERT INTO analysis_artifacts(content_object_id,analyser_name,analyser_version,
                 configuration_fingerprint,status,artifact_json)
               VALUES(?,'images','2','test','COMPLETED',?)""",
            # Two pairs, days apart: one event each, not one event of four.
            (content_id, json.dumps({"capture_time": base + (index // 2) * 86_400})),
        )
    connection.commit()

    assert run_photo_event_analysis(database, config)["photo_events"] == 2


def test_contact_sheets_are_not_re_rendered_when_nothing_changed(config, database, tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    from housekeeper.analysers.contact_sheets import (
        contact_sheet_path,
        run_contact_sheet_generation,
    )

    root = tmp_path / "src"
    root.mkdir()
    for index, colour in enumerate([(10, 120, 200), (12, 122, 202), (14, 124, 204)]):
        Image.new("RGB", (48, 48), colour).save(root / f"i{index}.png")
    _scan(config, database, root)
    run_content_analysis(database, config, "images")
    run_image_analysis(database, config)

    first = run_contact_sheet_generation(database, config)
    assert first["sheets_written"] >= 1 and first["sheets_reused"] == 0
    group_id = int(
        database.fetch_one(
            "SELECT id FROM relationship_groups WHERE group_type='IMAGE_SIMILARITY'"
        )["id"]
    )
    written_at = contact_sheet_path(config, group_id).stat().st_mtime_ns

    second = run_contact_sheet_generation(database, config)
    assert second["sheets_written"] == 0
    assert second["sheets_reused"] == first["sheets_written"]
    assert contact_sheet_path(config, group_id).stat().st_mtime_ns == written_at

    # A changed thumbnail is a changed input, so the sheet is rebuilt rather than trusted.
    thumbnails = sorted((config.workspace / ".housekeeper" / "thumbnails").glob("*.jpg"))
    Image.new("RGB", (64, 64), (240, 10, 10)).save(thumbnails[0])
    third = run_contact_sheet_generation(database, config)
    assert third["sheets_written"] >= 1


def _images_with_descriptors(config, database, tmp_path, descriptors: list[str]) -> list[int]:
    """A real scanned image corpus whose stored descriptors are then set to chosen values.

    Scoping runs through ``filesystem_entries``, so the content objects have to be genuinely
    linked to scanned files; only the descriptor itself is synthesised, to put an exact Hamming
    distance under test.
    """
    pytest.importorskip("PIL")
    from PIL import Image

    root = tmp_path / "src"
    root.mkdir()
    for index in range(len(descriptors)):
        Image.new("RGB", (32, 32), (index * 40 + 5, 60, 90)).save(root / f"p{index}.png")
    _scan(config, database, root)
    run_content_analysis(database, config, "images")

    connection = database.connect()
    ids = [
        int(row["content_object_id"])
        for row in database.fetch_all(
            "SELECT content_object_id FROM analysis_artifacts "
            "WHERE analyser_name='images' AND status='COMPLETED' ORDER BY content_object_id"
        )
    ]
    assert len(ids) == len(descriptors)
    for content_id, phash in zip(ids, descriptors):
        connection.execute(
            """UPDATE analysis_artifacts
               SET artifact_json=json_set(artifact_json,'$.perceptual_hash',?)
               WHERE analyser_name='images' AND content_object_id=?""",
            (phash, content_id),
        )
    connection.commit()
    return ids
