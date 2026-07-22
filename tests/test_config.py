"""Configuration tests: merge, validation, fingerprint, storage profile."""

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
    config = {**config, "archives": {**config["archives"], "timeout_seconds": -1}}
    with pytest.raises(ValueError):
        validate_config(config)


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
    assert profile["profile_name"] in {"hdd", "ssd", "network"}
    assert profile["scan_workers"] >= 1


def test_network_path_selects_network_profile(tmp_path):
    from pathlib import Path

    config = load_config(workspace_override=tmp_path)
    profile = performance_profile(config, Path("//server/share"))
    assert profile["profile_name"] == "network"
