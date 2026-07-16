import copy
import json
from dataclasses import dataclass
from hashlib import sha256
from pathlib import Path
from typing import Any

import yaml

from .path_utils import normalize_absolute_path

DEFAULTS: dict[str, Any] = {
    "workspace": {
        "database": "workspace/inventory.sqlite",
        "logs_dir": "workspace/logs",
        "reports_dir": "workspace/reports",
        "manifests_dir": "workspace/manifests",
    },
    "scanner": {
        "follow_symlinks": False,
        "include_hidden": True,
        "stay_on_filesystem": False,
        "batch_size": 500,
        "checkpoint_interval_seconds": 30,
        "max_file_size_for_content_analysis": 1073741824,
        "excluded_paths": [],
        "excluded_names": [],
    },
    "hashing": {
        "algorithm": "sha256",
        "quick_hash_chunk_bytes": 1048576,
        "quick_hash_middle_samples": 2,
        "full_hash_block_bytes": 8388608,
        "verify_bytewise_before_move": False,
    },
    "archives": {
        "max_members": 100000,
        "max_declared_uncompressed_bytes": 536870912000,
        "max_nested_depth": 1,
        "timeout_seconds": 60,
    },
    "documents": {
        "max_text_characters": 2000000,
        "store_full_text": False,
        "store_normalized_text": True,
        "content_analysis_timeout_seconds": 60,
    },
    "images": {
        "enable_perceptual_hashing": True,
        "max_pixels": 200000000,
        "create_contact_sheets": True,
    },
    "directory_overlap": {
        "minimum_files": 5,
        "minimum_bytes": 1048576,
        "containment_threshold": 0.90,
        "high_containment_threshold": 0.98,
    },
    "reporting": {
        "large_file_threshold_bytes": 1073741824,
        "redact_source_root_in_reports": False,
    },
}


@dataclass(frozen=True)
class AppConfig:
    data: dict[str, Any]
    workspace: Path

    @property
    def database_path(self) -> Path:
        return (
            self.workspace / self.data["workspace"]["database"]
            if not Path(self.data["workspace"]["database"]).is_absolute()
            else Path(self.data["workspace"]["database"])
        )

    def section(self, name: str) -> dict[str, Any]:
        return self.data[name]


def merge_configs(base: dict, override: dict) -> dict:
    result = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_configs(result[key], value)
        else:
            result[key] = value
    return result


def validate_config(config: dict) -> None:
    for section in config.values():
        if isinstance(section, dict):
            for key, value in section.items():
                if (
                    (
                        "bytes" in key
                        or "seconds" in key
                        or key in {"max_members", "batch_size"}
                    )
                    and isinstance(value, int)
                    and value < 0
                ):
                    raise ValueError(f"negative limit: {key}")
    if config["hashing"]["algorithm"].lower() not in {"sha256", "sha512", "blake2b"}:
        raise ValueError("unsupported hash algorithm")


def resolve_workspace_paths(config: AppConfig) -> AppConfig:
    return config


def load_config(
    config_path: Path | None = None, workspace_override: Path | None = None
) -> AppConfig:
    data = DEFAULTS
    if config_path:
        with Path(config_path).open(encoding="utf-8") as fh:
            data = merge_configs(DEFAULTS, yaml.safe_load(fh) or {})
    validate_config(data)
    workspace = normalize_absolute_path(workspace_override or Path.cwd())
    return AppConfig(data, workspace)


def config_fingerprint(config: AppConfig) -> str:
    return sha256(
        json.dumps(config.data, sort_keys=True, default=str).encode()
    ).hexdigest()
