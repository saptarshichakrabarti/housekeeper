"""Configuration tests: merge, validation, fingerprint, storage profile."""

from pathlib import Path

import pytest

from housekeeper.config import (
    config_fingerprint,
    load_config,
    merge_configs,
    performance_profile,
    validate_config,
)


def test_merge_is_deep_and_non_destructive():
    base = {"a": {"x": 1, "y": 2}, "b": 3}
    merged = merge_configs(base, {"a": {"y": 9}})
    assert merged == {"a": {"x": 1, "y": 9}, "b": 3}
    assert base["a"]["y"] == 2  # original untouched


def test_validate_rejects_negative_limit():
    config = load_config().data.copy()
    config = {**config, "graph": {**config["graph"], "hard_max_nodes": -1}}
    with pytest.raises(ValueError):
        validate_config(config)


def test_validate_rejects_an_unknown_key():
    """A key nothing reads is an error, not a silent no-op — the whole point of Phase 6."""
    config = load_config().data.copy()
    config = {**config, "scanner": {**config["scanner"], "follow_symlinks": True}}
    with pytest.raises(ValueError, match="scanner.follow_symlinks"):
        validate_config(config)


def test_validate_rejects_an_unknown_key_inside_a_named_profile():
    config = load_config().data.copy()
    profiles = {**config["performance"]["profiles"]}
    profiles["ssd"] = {**profiles["ssd"], "quick_hash_workers": 4}
    config = {**config, "performance": {**config["performance"], "profiles": profiles}}
    with pytest.raises(ValueError, match="quick_hash_workers"):
        validate_config(config)


def test_operator_named_profiles_and_overrides_are_allowed():
    config = load_config().data.copy()
    profiles = {**config["performance"]["profiles"], "nvme": {"full_hash_workers": 8, "parser_workers": 8}}
    config = {
        **config,
        "performance": {
            **config["performance"],
            "profiles": profiles,
            "overrides": {"full_hash_workers": 2},
        },
    }
    validate_config(config)  # no raise: the map keys are the operator's to choose


def test_validate_rejects_unknown_hash():
    config = load_config().data.copy()
    config = {**config, "hashing": {**config["hashing"], "algorithm": "md5"}}
    with pytest.raises(ValueError):
        validate_config(config)


def test_validate_rejects_graph_defaults_over_hard_limits():
    config = load_config().data.copy()
    config = {**config, "graph": {**config["graph"], "default_max_nodes": 999_999}}
    with pytest.raises(ValueError):
        validate_config(config)


def test_fingerprint_changes_with_config(tmp_path):
    base = load_config(workspace_override=tmp_path)
    other_file = tmp_path / "cfg.yaml"
    other_file.write_text("hashing:\n  algorithm: sha512\n", encoding="utf-8")
    changed = load_config(other_file, tmp_path)
    assert config_fingerprint(base) != config_fingerprint(changed)


def test_performance_profile_defaults_to_conservative(tmp_path):
    config = load_config(workspace_override=tmp_path)
    profile = performance_profile(config, None)
    assert profile["profile_name"] == "hdd"
    assert profile["full_hash_workers"] >= 1


def test_selecting_a_profile_actually_selects_it(tmp_path):
    """Top-level worker keys used to shadow the profile, so "ssd" still ran one hash worker."""
    config = load_config(workspace_override=tmp_path)
    config.section("performance")["storage_profile"] = "ssd"
    assert performance_profile(config)["full_hash_workers"] == 4


def test_overrides_depart_from_the_profile_by_name(tmp_path):
    config = load_config(workspace_override=tmp_path)
    config.section("performance")["storage_profile"] = "ssd"
    config.section("performance")["overrides"] = {"full_hash_workers": 2}
    profile = performance_profile(config)
    assert profile["full_hash_workers"] == 2 and profile["parser_workers"] == 4

    config.section("performance")["overrides"] = {"scan_workers": 4}
    with pytest.raises(ValueError, match="scan_workers"):
        performance_profile(config)


def test_network_path_selects_network_profile(tmp_path):
    from pathlib import Path

    config = load_config(workspace_override=tmp_path)
    profile = performance_profile(config, Path("//server/share"))
    assert profile["profile_name"] == "network"


# --- storage_profile: auto, from measurement ------------------------------------------------------
# The plan offered three ways to pick a profile. The probe that walked the tree and read 8 MiB was
# deleted (18.6 s of a 32 s scan). The path heuristic recognises network mounts and nothing else.
# This is the third: use what the drive actually achieved last time.


def test_auto_prefers_a_measured_throughput_over_the_path_heuristic():
    from housekeeper.config import SSD_BYTES_PER_SECOND, observed_profile

    config = load_config()
    config.section("performance")["storage_profile"] = "auto"
    # A local path the heuristic can only call "hdd".
    local = Path("/mnt/somedrive")
    assert performance_profile(config, local)["profile_name"] == "hdd"
    fast = performance_profile(config, local, SSD_BYTES_PER_SECOND * 1.5)
    assert fast["profile_name"] == "ssd"
    assert int(fast["full_hash_workers"]) == 4
    # A slow measurement must not *demote* below the heuristic, only decline to promote.
    assert observed_profile(1_000_000) is None
    assert performance_profile(config, local, 1_000_000)["profile_name"] == "hdd"


def test_a_measurement_does_not_override_an_explicit_profile():
    """An operator who names a profile means it; a measurement is only for `auto`."""
    from housekeeper.config import SSD_BYTES_PER_SECOND

    config = load_config()
    config.section("performance")["storage_profile"] = "hdd"
    profile = performance_profile(config, Path("/mnt/x"), SSD_BYTES_PER_SECOND * 10)
    assert profile["profile_name"] == "hdd"
    assert int(profile["full_hash_workers"]) == 1


def test_throughput_observations_round_trip_on_the_source_root(config, database, tmp_path):
    from housekeeper.scanner import DriveScanner, build_source_root_fingerprint

    root = tmp_path / "measured"
    root.mkdir()
    (root / "a.txt").write_text("hello", encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    fingerprint = build_source_root_fingerprint(root)

    assert database.observed_hash_throughput(fingerprint) is None
    database.record_hash_throughput(fingerprint, 512_000_000.0)
    database.connect().commit()
    assert database.observed_hash_throughput(fingerprint) == 512_000_000.0
    # Merged into device metadata, not replacing it.
    import json as _json

    metadata = _json.loads(
        database.fetch_one(
            "SELECT device_metadata_json j FROM source_roots WHERE source_fingerprint=?",
            (fingerprint,),
        )["j"]
    )
    assert metadata[database.OBSERVED_THROUGHPUT_KEY] == 512_000_000.0


def test_a_tiny_sample_is_not_recorded_as_a_measurement(config, database, tmp_path):
    """Elapsed time over a few KB is pool startup, not the drive."""
    from housekeeper.analysers.registry import _record_identity_throughput
    from housekeeper.scanner import DriveScanner, build_source_root_fingerprint

    root = tmp_path / "tiny"
    root.mkdir()
    (root / "a.txt").write_text("hello", encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    _record_identity_throughput(database, 4096, 0.001)
    database.connect().commit()
    assert database.observed_hash_throughput(build_source_root_fingerprint(root)) is None
