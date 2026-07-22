import copy
import json
import time
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
        "contact_sheet_columns": 4,
        "contact_sheet_cell_pixels": 160,
        "contact_sheet_max_members": 36,
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
    "incremental": {
        "enabled": True,
        "reuse_verified_content_links": True,
        "reuse_unchanged_entry_hashes": True,
        "detect_renames": True,
        "require_full_hash_for_content_reuse": True,
    },
    "content_store": {
        "store_normalized_text": True,
        "compress_text": True,
        "compression": "gzip",
        "store_image_thumbnails": True,
        "thumbnail_max_dimension": 512,
    },
    "chunking": {
        "enabled": False,
        "minimum_file_size_bytes": 134217728,
        "maximum_total_index_bytes": 10737418240,
        "common_chunk_frequency_cutoff": 10000,
        "minimum_overlap_bytes": 65536,
        "default_profile": "balanced",
        "profiles": {
            "balanced": {
                "minimum_chunk_size": 16384,
                "average_chunk_size": 65536,
                "maximum_chunk_size": 262144,
            },
            "large_binary": {
                "minimum_chunk_size": 65536,
                "average_chunk_size": 262144,
                "maximum_chunk_size": 1048576,
            },
        },
    },
    "document_similarity": {
        "enabled": True,
        "shingle_type": "word",
        "shingle_size": 5,
        "minhash_permutations": 128,
        "lsh_threshold": 0.75,
        "verification_threshold": 0.80,
        "minimum_tokens": 20,
    },
    "binary_similarity": {
        "tlsh_enabled": False,
        "ssdeep_enabled": False,
        "minimum_file_size_bytes": 512,
        "maximum_file_size_bytes": 536870912,
    },
    "collections": {
        "photo_event_gap_minutes": 90,
        "work_session_gap_hours": 8,
        "acquisition_batch_gap_minutes": 30,
    },
    "preservation": {
        "enabled": True,
        "precise_gps_enabled": False,
        "flag_legacy_formats": True,
        "flag_encrypted_unknowns": True,
    },
    "review_priority": {
        "weights": {
            "recoverable_bytes": 1.0,
            "redundancy_confidence": 1.0,
            "regeneration_confidence": 1.0,
            "loss_risk": -2.0,
            "preservation_risk": -2.0,
            "review_effort": -0.5,
        }
    },
    "learning": {
        "enabled": False,
        "minimum_training_examples": 20,
        "model_type": "logistic_regression",
        "allow_protected_categories": False,
    },
    "normalization": {
        "office": {
            "enabled": True,
            "preserve_tracked_changes": True,
            "preserve_comments": True,
            "ignore_volatile_properties": True,
        },
        "pdf": {
            "enabled": False,
            "compare_page_text": True,
            "compare_embedded_images": True,
            "render_pages_by_default": False,
        },
        "images": {
            "decoded_pixel_hash": True,
            "orientation_normalized_hash": True,
            "preserve_metadata_differences": True,
        },
        "archives": {"enabled": True, "max_content_bytes": 268435456},
    },
    "dashboard": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8765,
        "open_browser": True,
        "read_only": False,
        "page_size": 100,
        "maximum_page_size": 500,
        "allow_non_loopback": False,
        "csrf_enabled": True,
        "show_document_excerpts": False,
        "maximum_excerpt_characters": 5000,
    },
    "graph": {
        "enabled": True,
        "default_projection": "universe",
        "default_max_nodes": 500,
        "hard_max_nodes": 5000,
        "default_max_edges": 2000,
        "hard_max_edges": 20000,
        "minimum_edge_confidence": 0.70,
        "cache_layouts": True,
        "allow_raw_file_nodes": False,
    },
    "performance": {
        "storage_profile": "auto",
        "profiles": {
            "hdd": {
                "scan_workers": 1,
                "full_hash_workers": 1,
                "quick_hash_workers": 1,
                "parser_workers": 2,
            },
            "ssd": {
                "scan_workers": 2,
                "full_hash_workers": 4,
                "quick_hash_workers": 2,
                "parser_workers": 4,
            },
            "network": {
                "scan_workers": 1,
                "full_hash_workers": 1,
                "quick_hash_workers": 1,
                "parser_workers": 2,
            },
        },
        "batch_size": 1000,
        "database_writer_queue_size": 10000,
        "parser_workers": 2,
        "parser_timeout_seconds": 60,
        "parser_memory_limit_mb": 1024,
        "full_hash_workers": 1,
        "quick_hash_workers": 2,
        "progress_interval_seconds": 5,
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
                    ("bytes" in key or "seconds" in key or key in {"max_members", "batch_size"})
                    and isinstance(value, int)
                    and value < 0
                ):
                    raise ValueError(f"negative limit: {key}")
    if config["hashing"]["algorithm"].lower() not in {"sha256", "sha512", "blake2b"}:
        raise ValueError("unsupported hash algorithm")
    if (
        config["graph"]["default_max_nodes"] > config["graph"]["hard_max_nodes"]
        or config["graph"]["default_max_edges"] > config["graph"]["hard_max_edges"]
    ):
        raise ValueError("graph defaults exceed hard limits")
    if config["dashboard"]["page_size"] > config["dashboard"]["maximum_page_size"]:
        raise ValueError("dashboard page size exceeds maximum")


def resolve_workspace_paths(config: AppConfig) -> AppConfig:
    return config


def load_config(
    config_path: Path | None = None, workspace_override: Path | None = None
) -> AppConfig:
    # Deep-copy so callers that mutate a config section never corrupt the shared DEFAULTS.
    data = copy.deepcopy(DEFAULTS)
    if config_path:
        with Path(config_path).open(encoding="utf-8") as fh:
            data = merge_configs(DEFAULTS, yaml.safe_load(fh) or {})
    validate_config(data)
    workspace = normalize_absolute_path(workspace_override or Path.cwd())
    return AppConfig(data, workspace)


def config_fingerprint(config: AppConfig) -> str:
    return sha256(json.dumps(config.data, sort_keys=True, default=str).encode()).hexdigest()


def performance_profile(config: AppConfig, source_root: Path | None = None) -> dict[str, int | str]:
    """Return a measured conservative worker profile without modifying the source."""
    performance = config.section("performance")
    profile = str(performance.get("storage_profile", "auto")).lower()
    if profile == "auto":
        profile = _measure_storage_profile(source_root) if source_root else "hdd"
    if profile not in performance["profiles"]:
        raise ValueError(f"unknown storage profile: {profile}")
    selected = dict(performance["profiles"][profile])
    for key in ("full_hash_workers", "quick_hash_workers", "parser_workers"):
        override = performance.get(key, selected[key])
        selected[key] = int(override if override is not None else selected[key])
    selected["scan_workers"] = int(selected["scan_workers"])
    selected["profile_name"] = profile
    return selected


def _measure_storage_profile(source_root: Path | None) -> str:
    if source_root is None:
        return "hdd"
    root_text = str(source_root).lower()
    if root_text.startswith(("//", "\\\\", "/net/", "/nfs/", "/smb/")):
        return "network"
    # Sample at most 8 MiB from existing files. Errors and small samples retain the
    # conservative HDD profile; this must never write a benchmark file to the source.
    read_bytes = 0
    started = time.perf_counter()
    try:
        for candidate in source_root.rglob("*"):
            if not candidate.is_file():
                continue
            with candidate.open("rb") as handle:
                data = handle.read(min(2 * 1024 * 1024, candidate.stat().st_size))
            read_bytes += len(data)
            if read_bytes >= 8 * 1024 * 1024:
                break
    except OSError:
        return "hdd"
    elapsed = time.perf_counter() - started
    if read_bytes < 1_024 * 1_024 or elapsed <= 0:
        return "hdd"
    return "ssd" if read_bytes / elapsed >= 150 * 1024 * 1024 else "hdd"
