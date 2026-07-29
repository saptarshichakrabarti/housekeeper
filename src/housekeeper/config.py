"""Configuration: every key here is read by something, and nothing else is accepted.

Two rules make that true rather than aspirational:

* **An unknown key is an error.** A knob that no longer exists used to merge through silently, so
  an operator editing a stale config believed they had changed something. :func:`validate_config`
  now rejects it by name.
* **Nothing is listed that is not wired.** Keys describing behaviour the code does not have were
  removed rather than documented, because a setting an operator reasonably believes in is worse
  than a missing one. ``CHANGELOG.md`` records what went and why.
"""

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
    },
    "scanner": {
        # Entries staged per transaction during traversal: the bound on how much an interrupted
        # scan has to redo, and on how much sits in memory before it is written.
        "batch_size": 5000,
        "stay_on_filesystem": False,
        "max_file_size_for_content_analysis": 1073741824,
        "excluded_paths": [],
        "excluded_names": [],
    },
    "hashing": {
        "algorithm": "sha256",
        "quick_hash_chunk_bytes": 1048576,
        "quick_hash_middle_samples": 2,
        "full_hash_block_bytes": 8388608,
    },
    "archives": {
        # Nested archives are inventoried, never expanded (decompression-bomb safety), so there is
        # no depth to configure.
        "max_members": 100000,
        "max_declared_uncompressed_bytes": 536870912000,
    },
    "documents": {
        "max_text_characters": 2000000,
        "store_normalized_text": True,
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
    },
    "reporting": {
        "large_file_threshold_bytes": 1073741824,
        # Replace the mount path in report and export output with "<source>", leaving the
        # source-relative path. Reports are static HTML that gets copied and shared; an absolute
        # path carries the account name and directory layout of the machine that produced it, and
        # the relative path is what a reader actually needs.
        #
        # Off by default: an operator triaging their own drive wants a path they can paste into a
        # terminal. It is a real switch either way, which the key it replaces
        # (`redact_source_root_in_reports`) never was — that one was read by nothing and reports
        # always contained full paths.
        #
        # Deliberately does NOT apply to review manifests. A manifest is the movement contract; it
        # is revalidated by absolute path and hash immediately before a file is moved, so redacting
        # it would break the one operation this tool performs. See CHANGELOG.md.
        "redact_source_paths": False,
    },
    "incremental": {
        # Reuse of unchanged signatures and content links is not optional: it is what makes a
        # rescan proportional to what changed. Only the rename heuristic is a real choice.
        "detect_renames": True,
        # Reuse of whole pipeline stages whose inputs (snapshot content, configuration, code) are
        # unchanged since a completed run. `quickstart --full` overrides it for one run.
        "reuse_unchanged_stages": True,
    },
    # A shell command a *scheduled* run pipes the "what changed" digest into (notify-send, mail, a
    # curl to a webhook). Empty means no notification. Housekeeper never talks to the network itself
    # and never runs this command outside a scheduler unit it printed — see schedules.py.
    "notifications": {"command": ""},
    "content_store": {
        "store_normalized_text": True,
        "compress_text": True,
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
        "minimum_file_size_bytes": 512,
        "maximum_file_size_bytes": 536870912,
    },
    "collections": {
        "photo_event_gap_minutes": 90,
        "work_session_gap_hours": 8,
        "acquisition_batch_gap_minutes": 30,
    },
    # Precise GPS is never read, stored or displayed by any code path, so there is no knob for it.
    "preservation": {"enabled": True},
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
    # The normalization profiles' own semantics are fixed and versioned in
    # housekeeper.normalization.* — they are part of each profile's configuration fingerprint, so
    # they cannot be config keys without invalidating stored artifacts. Only the on/off switch and
    # the size bound belong here.
    "normalization": {
        "office": {"enabled": True},
        "pdf": {"enabled": False},
        "images": {"decoded_pixel_hash": True, "orientation_normalized_hash": True},
        "archives": {"enabled": True, "max_content_bytes": 268435456},
    },
    # CSRF validation and loopback binding are unconditional; document excerpts are never rendered.
    # There is deliberately no switch to weaken any of the three.
    "dashboard": {
        "enabled": True,
        "host": "127.0.0.1",
        "port": 8765,
        "open_browser": True,
        "read_only": False,
        "page_size": 100,
        "maximum_page_size": 500,
        "allow_non_loopback": False,
    },
    "graph": {
        "enabled": True,
        "default_max_nodes": 500,
        "hard_max_nodes": 5000,
        "default_max_edges": 2000,
        "hard_max_edges": 20000,
        "minimum_edge_confidence": 0.70,
    },
    "performance": {
        "storage_profile": "auto",
        # Worker counts live here and nowhere else. Top-level duplicates of these keys used to
        # shadow the profile unconditionally, so selecting "ssd" still ran full_hash_workers=1;
        # an operator who wants to depart from the profile now says so in `overrides`.
        "profiles": {
            "hdd": {"full_hash_workers": 1, "parser_workers": 2},
            "ssd": {"full_hash_workers": 4, "parser_workers": 4},
            "network": {"full_hash_workers": 1, "parser_workers": 2},
        },
        "overrides": {},
        "batch_size": 1000,
        "database_writer_queue_size": 10000,
        "parser_timeout_seconds": 60,
        "parser_memory_limit_mb": 1024,
    },
}

#: Maps whose keys the operator names. The value shape is still checked, against the template.
_TEMPLATED_MAPS = {
    ("performance", "profiles"): ("hdd",),
    ("chunking", "profiles"): ("balanced",),
}
#: Maps whose keys are validated where they are used, not against a fixed schema.
_FREE_FORM_MAPS = {("performance", "overrides")}


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


def _unknown_keys(data: dict, schema: dict, prefix: tuple[str, ...] = ()) -> list[str]:
    """Every key in ``data`` that ``schema`` does not define, fully qualified.

    A stale key used to merge straight through, so a config still setting a knob that was removed
    (or misspelling one that exists) looked like it had taken effect. This is what makes the
    "every key here is read by something" claim in the module docstring enforceable.
    """
    unknown: list[str] = []
    for key, value in data.items():
        path = prefix + (str(key),)
        if key not in schema:
            unknown.append(".".join(path))
            continue
        if not isinstance(value, dict) or not isinstance(schema[key], dict):
            continue
        if path in _FREE_FORM_MAPS:
            continue
        if path in _TEMPLATED_MAPS:
            template_name = _TEMPLATED_MAPS[path][0]
            template = schema[key][template_name]
            for name, entry in value.items():
                if isinstance(entry, dict):
                    unknown.extend(_unknown_keys(entry, template, path + (str(name),)))
            continue
        unknown.extend(_unknown_keys(value, schema[key], path))
    return unknown


def validate_config(config: dict) -> None:
    unknown = _unknown_keys(config, DEFAULTS)
    if unknown:
        raise ValueError(
            "unknown configuration key(s): "
            + ", ".join(sorted(unknown))
            + " — see CHANGELOG.md for keys removed because nothing read them"
        )
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


#: Sustained hashing throughput above which storage is treated as solid-state. Rotational disks and
#: network shares do not reach this on a mixed corpus once seek time is included; an SSD clears it
#: comfortably. Deliberately well clear of both, because the penalty for guessing wrong is real work.
SSD_BYTES_PER_SECOND = 200_000_000


def observed_profile(bytes_per_second: float | None) -> str | None:
    """Which profile a measured hashing throughput implies, or None if it implies nothing."""
    if not bytes_per_second or bytes_per_second <= 0:
        return None
    return "ssd" if bytes_per_second >= SSD_BYTES_PER_SECOND else None


def performance_profile(
    config: AppConfig,
    source_root: Path | None = None,
    measured_bytes_per_second: float | None = None,
) -> dict[str, int | str]:
    """The worker counts for this source root: the profile, then any explicit overrides.

    ``measured_bytes_per_second`` is what a *previous* run of this source actually achieved. It is
    the third option the optimisation plan left open — adapt from real hash operations rather than
    from a probe — and it is applied one run late on purpose. Measuring at the start of a run is what
    the deleted 18.6-second ``rglob`` probe did; measuring during a run and resizing a live worker
    pool means the number that sized the work in flight changes under it. Recording what happened and
    using it next time costs nothing and converges after one scan.
    """
    performance = config.section("performance")
    profile = str(performance.get("storage_profile", "auto")).lower()
    if profile == "auto":
        profile = observed_profile(measured_bytes_per_second) or _profile_from_path(source_root)
    if profile not in performance["profiles"]:
        raise ValueError(f"unknown storage profile: {profile}")
    selected: dict[str, int | str] = {
        key: int(value) for key, value in performance["profiles"][profile].items()
    }
    for key, value in (performance.get("overrides") or {}).items():
        if key not in selected:
            raise ValueError(
                f"unknown performance override: {key} (expected one of {sorted(selected)})"
            )
        selected[key] = int(value)
    selected["profile_name"] = profile
    return selected


def _profile_from_path(source_root: Path | None) -> str:
    """Classify storage from the path alone. Never reads the drive.

    This replaces a probe that walked the whole tree with ``rglob`` and opened files until it had
    read 8 MiB — measured at **18.6 s of a 32 s scan** on a tree of small files, because a byte
    budget is unreachable when every file is tiny. Tuning that costs more than the work it tunes
    is not tuning.

    A path heuristic can only recognise network mounts, so everything else gets the conservative
    profile. That is the honest trade: an operator on an SSD sets ``performance.storage_profile:
    ssd`` and gets four hash workers, and one who sets nothing is merely slower, never wrong.
    """
    if source_root is None:
        return "hdd"
    text = str(source_root).lower()
    if text.startswith(("//", "\\\\", "/net/", "/nfs/", "/smb/")):
        return "network"
    return "hdd"
