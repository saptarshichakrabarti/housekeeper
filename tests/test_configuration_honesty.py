"""Phase 6: every configuration key is read, and the ones that were not are gone.

The first test is the durable one — it fails when somebody adds a knob without wiring it, which is
how the previous twenty-nine accumulated. The rest assert that the settings wired in this phase
actually change behaviour, because "it is referenced somewhere" is a weaker claim than "it works".
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from housekeeper.analysers.images import run_image_analysis
from housekeeper.analysers.registry import run_content_analysis
from housekeeper.config import DEFAULTS
from housekeeper.scanner import DriveScanner

SOURCE = Path(__file__).resolve().parents[1] / "src"

#: Keys whose *name* is not what the code greps for, with where they are actually consumed.
_READ_INDIRECTLY = {
    # The operator names these; the schema check validates them against a template.
    "performance.profiles.hdd.full_hash_workers",
    "performance.profiles.hdd.parser_workers",
    "performance.profiles.ssd.full_hash_workers",
    "performance.profiles.ssd.parser_workers",
    "performance.profiles.network.full_hash_workers",
    "performance.profiles.network.parser_workers",
    "performance.overrides",
    "chunking.profiles.balanced.minimum_chunk_size",
    "chunking.profiles.balanced.average_chunk_size",
    "chunking.profiles.balanced.maximum_chunk_size",
    "chunking.profiles.large_binary.minimum_chunk_size",
    "chunking.profiles.large_binary.average_chunk_size",
    "chunking.profiles.large_binary.maximum_chunk_size",
}


def _leaves(node, prefix=()):
    for key, value in node.items():
        if isinstance(value, dict) and value:
            yield from _leaves(value, prefix + (key,))
        else:
            yield ".".join(prefix + (key,)), value


def test_every_configuration_key_is_read_by_something():
    """A setting an operator can change must change something.

    Name-based, so it is a floor rather than a proof: a key that appears only in a comment would
    pass. It still catches the failure that actually happened — a knob added to DEFAULTS, described
    in the documentation, and never once read.
    """
    unread = []
    for name, _ in _leaves(DEFAULTS):
        if name in _READ_INDIRECTLY:
            continue
        found = subprocess.run(
            ["grep", "-rl", "--include=*.py", name.rsplit(".", 1)[-1], str(SOURCE)],
            capture_output=True,
            text=True,
            check=False,  # grep exits 1 on no match, which is the interesting case
        ).stdout.splitlines()
        if not [path for path in found if not path.endswith("/config.py")]:
            unread.append(name)
    assert unread == [], f"configuration keys nothing reads: {unread}"


def test_scan_batch_size_is_the_transaction_size(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    for index in range(20):
        (root / f"f{index}.txt").write_text(str(index))
    config.section("scanner")["batch_size"] = 3
    counts = DriveScanner(database, config).scan(root, incremental=False)
    assert counts["files"] == 20  # a smaller transaction changes durability, never the inventory


def test_stay_on_filesystem_stops_at_a_device_boundary(config, database, tmp_path, monkeypatch):
    """Recorded, but not descended into: a mount point belongs to this drive, its contents do not."""
    root = tmp_path / "src"
    (root / "mounted").mkdir(parents=True)
    (root / "here.txt").write_text("on this device")
    (root / "mounted" / "elsewhere.txt").write_text("on another device")

    real_inspect = DriveScanner.inspect_entry

    def fake_inspect(self, path, scan_root, *args, **kwargs):
        record = real_inspect(self, path, scan_root, *args, **kwargs)
        if path.name == "mounted":
            object.__setattr__(record, "device_id", (record.device_id or 0) + 1)
        return record

    monkeypatch.setattr(DriveScanner, "inspect_entry", fake_inspect)
    config.section("scanner")["stay_on_filesystem"] = True
    DriveScanner(database, config).scan(root, incremental=False)
    names = {row["name"] for row in database.fetch_all("SELECT name FROM filesystem_entries")}
    assert "mounted" in names and "here.txt" in names
    assert "elsewhere.txt" not in names


def test_maximum_analysis_file_size_excludes_a_large_file(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "small.txt").write_text("x" * 10)
    (root / "large.txt").write_text("y" * 5000)
    DriveScanner(database, config).scan(root, incremental=False)
    config.section("scanner")["max_file_size_for_content_analysis"] = 100
    run_content_analysis(database, config, "documents")
    analysed = {
        Path(row["absolute_path"]).name
        for row in database.fetch_all(
            """SELECT e.absolute_path FROM analysis_artifacts a
               JOIN entry_content_links l ON l.content_object_id=a.content_object_id
               JOIN filesystem_entries e ON e.id=l.entry_id WHERE a.analyser_name='documents'"""
        )
    }
    assert analysed == {"small.txt"}


def test_disabling_perceptual_hashing_removes_the_descriptor(config, database, tmp_path):
    pytest.importorskip("PIL")
    from PIL import Image

    root = tmp_path / "src"
    root.mkdir()
    for index, colour in enumerate([(10, 120, 200), (12, 122, 202)]):
        Image.new("RGB", (48, 48), colour).save(root / f"i{index}.png")
    DriveScanner(database, config).scan(root, incremental=False)
    config.section("images")["enable_perceptual_hashing"] = False
    run_content_analysis(database, config, "images")
    run_image_analysis(database, config)
    assert database.fetch_one("SELECT COUNT(*) AS n FROM image_phash_bands")["n"] == 0
    assert database.fetch_all(
        "SELECT 1 FROM relationship_groups WHERE group_type='IMAGE_SIMILARITY'"
    ) == []
    # Dimensions and capture time are still recorded: only the fuzzy signal is switched off.
    assert database.fetch_one(
        "SELECT COUNT(*) AS n FROM analysis_artifacts WHERE analyser_name='images' AND status='COMPLETED'"
    )["n"] == 2


def test_detect_renames_can_be_switched_off(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "before.txt").write_text("a stable body of text, long enough to be worth matching")
    scanner = DriveScanner(database, config)
    scanner.scan(root, incremental=False)
    (root / "before.txt").rename(root / "after.txt")

    config.section("incremental")["detect_renames"] = False
    scanner.scan(root, incremental=True, resume=False)
    assert database.fetch_one(
        "SELECT COUNT(*) AS n FROM scan_entry_changes WHERE change_status='MOVED_OR_RENAMED_CANDIDATE'"
    )["n"] == 0


def test_graph_limits_come_from_the_configuration(database, config):
    from housekeeper.graph.projections import projection_limits

    config.section("graph")["default_max_nodes"] = 42
    config.section("graph")["default_max_edges"] = 43
    assert projection_limits(config) == (42, 43)
    with pytest.raises(ValueError, match="hard limits"):
        projection_limits(config, requested_nodes=999_999)


def test_configured_worker_counts_are_what_actually_run(config, tmp_path):
    """Definition of done #9: the worker counts are observable, not just settable.

    ``parser_workers`` was famously never read by the parser loop at all. The counter records
    process starts where processes are actually created, so this asserts the number the operator
    configured is the number that exists.
    """
    from housekeeper.analysers.parser_pool import ParserPool, worker_count
    from housekeeper.core import counters

    config.section("performance")["storage_profile"] = "ssd"
    config.section("performance")["overrides"] = {"parser_workers": 3}
    assert worker_count(config) == 3

    document = tmp_path / "note.txt"
    document.write_text("observable", encoding="utf-8")
    with counters.recording() as counts:
        pool = ParserPool(config, worker_count(config))
        try:
            result = pool.run("documents", str(document), 30)
            assert result.get("analysis_status", result.get("extraction_status")) != "ERROR"
        finally:
            pool.close()
    assert counts["parser_processes_started"] == 3, (
        f"configured 3 parser workers, started {counts['parser_processes_started']}"
    )
