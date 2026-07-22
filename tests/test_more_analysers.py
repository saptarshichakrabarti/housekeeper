"""Audio/tabular normalization, archive-vs-directory, acquisition batches, retention."""

import os
import time
import zipfile

from housekeeper.analyzers.archive_equivalence import run_archive_directory_analysis
from housekeeper.analyzers.exact_duplicates import run_exact_duplicate_analysis
from housekeeper.analyzers.normalized_content import run_normalized_content_analysis
from housekeeper.collections.events import run_acquisition_batch_analysis
from housekeeper.collections.record_series import run_record_series_analysis
from housekeeper.collections.retention import apply_retention_policies
from housekeeper.normalization.audio import normalize_audio
from housekeeper.normalization.tabular import normalize_tabular
from housekeeper.scanner import DriveScanner


def _syncsafe(n: int) -> bytes:
    return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])


def _mp3(tag: bytes, payload: bytes) -> bytes:
    return b"ID3\x03\x00\x00" + _syncsafe(len(tag)) + tag + payload


def test_audio_payload_ignores_tags(config, tmp_path):
    payload = bytes(range(256)) * 200
    a = tmp_path / "a.mp3"
    b = tmp_path / "b.mp3"
    a.write_bytes(_mp3(b"artist=Alpha album=One", payload))
    b.write_bytes(_mp3(b"artist=Completely Different Longer Tag Value", payload))
    first = normalize_audio(a, config)
    second = normalize_audio(b, config)
    assert first.status == "OK"
    assert first.normalized_hash == second.normalized_hash  # same audio payload, different tags


def test_audio_tag_variant_relationship(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    payload = bytes(range(256)) * 300
    (root / "song-v1.mp3").write_bytes(_mp3(b"tag one", payload))
    (root / "song-v2.mp3").write_bytes(_mp3(b"a much longer different tag two", payload))
    DriveScanner(database, config).scan(root, incremental=False)
    run_normalized_content_analysis(database, config)
    rels = database.fetch_all(
        "SELECT relationship_type,evidence_tier FROM content_relationships WHERE relationship_type='AUDIO_TAG_VARIANT'"
    )
    assert len(rels) == 1
    assert rels[0]["evidence_tier"] == "TIER_2_NORMALIZED_EXACT"


def test_tabular_equivalence_ignores_quoting(config, tmp_path):
    a = tmp_path / "a.csv"
    b = tmp_path / "b.csv"
    c = tmp_path / "c.csv"
    a.write_text("name,value\nalpha,1\nbeta,2\n", encoding="utf-8")
    b.write_text('"name","value"\r\n"alpha"," 1 "\r\n"beta","2"\r\n', encoding="utf-8")  # same cells
    c.write_text("name,value\nalpha,1\ngamma,9\n", encoding="utf-8")  # different
    assert normalize_tabular(a, config).normalized_hash == normalize_tabular(b, config).normalized_hash
    assert normalize_tabular(a, config).normalized_hash != normalize_tabular(c, config).normalized_hash


def test_archive_of_directory(config, database, tmp_path):
    root = tmp_path / "src"
    data = root / "Data"
    data.mkdir(parents=True)
    (data / "a.txt").write_text("content alpha", encoding="utf-8")
    (data / "b.txt").write_text("content beta", encoding="utf-8")
    with zipfile.ZipFile(root / "snapshot.zip", "w") as archive:
        archive.writestr("a.txt", "content alpha")
        archive.writestr("b.txt", "content beta")
    DriveScanner(database, config).scan(root, incremental=False)
    result = run_archive_directory_analysis(database, config)
    assert result["relationships"] >= 1
    rel = database.fetch_one(
        "SELECT relationship_type,confidence FROM content_relationships WHERE relationship_type='ARCHIVE_OF_DIRECTORY'"
    )
    assert rel is not None
    assert rel["confidence"] == 1.0


def test_acquisition_batches(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    base = time.time()
    for i in range(3):
        path = root / f"download{i}.bin"
        path.write_bytes(bytes([i]) * 100)
        os.utime(path, (base + i * 30, base + i * 30))  # within 30-minute gap
    DriveScanner(database, config).scan(root, incremental=False)
    result = run_acquisition_batch_analysis(database, config)
    assert result["acquisition_batches"] == 1


def test_binary_similarity_capability_report(config, database, tmp_path):
    from housekeeper.analyzers.binary_similarity import run_binary_similarity_analysis
    from housekeeper.similarity.fuzzy_hashes import capabilities

    caps = capabilities()
    assert set(caps) == {"TLSH_AVAILABLE", "SSDEEP_AVAILABLE"}
    assert all(isinstance(v, bool) for v in caps.values())
    # Without the optional backend enabled, the analyzer reports availability and fabricates
    # no relationships (a fuzzy match must never become an exact classification).
    result = run_binary_similarity_analysis(database, config)
    assert result["relationships"] == 0
    assert "TLSH_AVAILABLE" in result
    assert database.fetch_one("SELECT COUNT(*) n FROM content_relationships")["n"] == 0


def test_binary_similarity_enabled_without_backend_is_graceful(config, database):
    from housekeeper.analyzers.binary_similarity import run_binary_similarity_analysis

    config.section("binary_similarity")["tlsh_enabled"] = True
    result = run_binary_similarity_analysis(database, config)
    # Enabling the flag without the native dep must not crash; it reports unavailable.
    assert result["status"] in {"unavailable", "ok"}


def test_pdf_equivalence_alias_runs(config, database, tmp_path):
    from housekeeper.analyzers.normalized_content import run_normalized_content_analysis

    root = tmp_path / "src"
    root.mkdir()
    (root / "a.txt").write_text("x", encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    # pdf-equivalence is handled by the normalized-content analyzer path.
    assert isinstance(run_normalized_content_analysis(database, config), dict)


def test_retention_application_summary(config, database, tmp_path):
    root = tmp_path / "src"
    proj = root / "Project"
    proj.mkdir(parents=True)
    (proj / "main.py").write_text("print('x')", encoding="utf-8")
    (proj / "dist").mkdir()
    (proj / "dist" / "bundle.js").write_bytes(b"generated")
    DriveScanner(database, config).scan(root, incremental=False)
    run_exact_duplicate_analysis(database, config)
    from housekeeper.policies import classify_all_entries

    classify_all_entries(database, config)
    run_record_series_analysis(database, config)
    summary = apply_retention_policies(database, config)
    assert isinstance(summary, dict)
    # A retention policy is linked to at least one series.
    assert database.fetch_one("SELECT COUNT(*) n FROM retention_policies")["n"] >= 2
    assert database.fetch_one(
        "SELECT COUNT(*) n FROM record_series WHERE retention_policy_id IS NOT NULL"
    )["n"] >= 1
