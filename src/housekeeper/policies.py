"""Deterministic policy engine: analysis facts to conservative classifications.

Rules from ``cleanup_rules.yaml`` (or builtins); conditions are code, not an eval language.
Conflicts: most protective wins. Fail closed: inspect failures become ``ERROR``, never review-safe.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import AppConfig
from .constants import PROTECTED_SUFFIXES, Classification
from .database import Database
from .jobs import checkpoint, update_job

# Most-protective first.  Index in this list is the classification's protective rank.
DEFAULT_PRIORITY: list[str] = [
    Classification.ERROR,
    Classification.PROTECTED,
    Classification.UNKNOWN,
    Classification.KEEP,
    Classification.KEEP_CANONICAL,
    Classification.REVIEW_VERSION_FAMILY,
    Classification.REVIEW_BACKUP,
    Classification.REVIEW_LARGE,
    Classification.REVIEW_PROBABLE,
    Classification.REVIEW_SAFE,
]

DEFAULT_RULES: list[dict[str, Any]] = [
    {
        "id": "parser-or-filesystem-error",
        "condition": "analysis_or_scan_failed",
        "classification": Classification.ERROR,
        "confidence": 1.0,
        "reason_codes": ["PARSER_OR_FILESYSTEM_ERROR"],
        "explanation": "Inspection or content analysis failed; manual review is required.",
        "requires_manual_approval": True,
    },
    {
        "id": "protected-signal",
        "condition": "protected_signal",
        "classification": Classification.PROTECTED,
        "confidence": 0.95,
        "reason_codes": ["PROTECTED_SIGNAL"],
        "explanation": "Matches a conservative protected-file pattern (extension, name or directory).",
        "requires_manual_approval": True,
    },
    {
        "id": "exact-duplicate-noncanonical",
        "condition": "exact_duplicate_noncanonical",
        "classification": Classification.REVIEW_SAFE,
        "confidence": 1.0,
        "reason_codes": ["EXACT_SHA256_DUPLICATE", "VERIFIED_CANONICAL_SURVIVES"],
        "explanation": "Verified byte-identical duplicate; a canonical copy remains outside review.",
        "requires_manual_approval": True,
        "requires_canonical": True,
    },
    {
        "id": "office-temporary-lock",
        "condition": "office_temporary_lock",
        "classification": Classification.REVIEW_SAFE,
        "confidence": 0.9,
        "reason_codes": ["OFFICE_TEMPORARY_LOCK_FILE"],
        "explanation": "Office temporary owner-lock file with a matching document nearby.",
        "requires_manual_approval": True,
    },
    {
        "id": "python-bytecode-cache",
        "condition": "python_bytecode_cache",
        "classification": Classification.REVIEW_SAFE,
        "confidence": 0.9,
        "reason_codes": ["PYTHON_BYTECODE_CACHE", "REGENERABLE"],
        "explanation": "Compiled Python cache inside a source project; regenerated automatically.",
        "requires_manual_approval": True,
    },
    {
        "id": "virtualenv-regenerable",
        "condition": "virtualenv_with_reproducibility",
        "classification": Classification.REVIEW_PROBABLE,
        "confidence": 0.75,
        "reason_codes": ["VIRTUAL_ENVIRONMENT", "PROJECT_HAS_REPRODUCIBILITY"],
        "explanation": "Virtual-environment content inside a project that carries a dependency or lock specification.",
        "requires_manual_approval": True,
    },
    {
        "id": "node-modules-regenerable",
        "condition": "node_modules_with_lockfile",
        "classification": Classification.REVIEW_PROBABLE,
        "confidence": 0.75,
        "reason_codes": ["NODE_MODULES", "PROJECT_HAS_LOCKFILE"],
        "explanation": "node_modules content inside a project that carries a lockfile.",
        "requires_manual_approval": True,
    },
    {
        "id": "old-duplicate-installer",
        "condition": "old_duplicate_installer",
        "classification": Classification.REVIEW_PROBABLE,
        "confidence": 0.6,
        "reason_codes": ["INSTALLER_OR_IMAGE", "OLD_AND_DUPLICATED"],
        "explanation": "Old installer or disk image that is byte-duplicated elsewhere. Age alone is never sufficient.",
        "requires_manual_approval": True,
    },
]

DEFAULT_POLICY: dict[str, Any] = {
    "classification": Classification.KEEP,
    "confidence": 0.5,
    "reason_codes": ["NO_REMOVAL_SIGNAL"],
    "explanation": "No safe-removal signal detected; retained by default.",
    "requires_manual_approval": False,
}

_DEPENDENCY_SPECS = {
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "environment.yml",
    "environment.yaml",
    "Pipfile",
    "package.json",
    "Cargo.toml",
    "go.mod",
}
_LOCKFILES = {
    "poetry.lock",
    "Pipfile.lock",
    "uv.lock",
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "Cargo.lock",
    "go.sum",
}
_INSTALLER_SUFFIXES = {
    ".exe",
    ".msi",
    ".dmg",
    ".pkg",
    ".deb",
    ".rpm",
    ".iso",
    ".img",
    ".appimage",
}
_INSTALLER_AGE_SECONDS = 365 * 24 * 3600


@dataclass(frozen=True)
class PolicyRule:
    id: str
    condition: str
    classification: str
    confidence: float
    reason_codes: list[str]
    explanation: str
    requires_manual_approval: bool = True
    requires_canonical: bool = False
    params: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ClassificationResult:
    entry_id: int
    classification: str
    confidence: float
    primary_reason_code: str
    reason_codes: list[str]
    rule_ids: list[str]
    explanation: str
    canonical_entry_id: int | None
    requires_manual_approval: bool

    def to_row(self) -> tuple[Any, ...]:
        return (
            self.entry_id,
            self.classification,
            self.confidence,
            self.primary_reason_code,
            json.dumps(self.reason_codes),
            json.dumps(self.rule_ids),
            self.explanation,
            self.canonical_entry_id,
            int(self.requires_manual_approval),
        )


@dataclass(frozen=True)
class ProtectedConfig:
    suffixes: frozenset[str]
    filenames: frozenset[str]
    directory_names: frozenset[str]


def _candidate_config_paths(config: AppConfig | None, filename: str):
    """Search the workspace, the CWD, and the source-tree ``config/`` directory."""
    if config is not None:
        yield config.workspace / "config" / filename
    yield Path.cwd() / "config" / filename
    yield Path(__file__).resolve().parents[2] / "config" / filename


def _load_yaml(config: AppConfig | None, filename: str) -> dict[str, Any] | None:
    for candidate in _candidate_config_paths(config, filename):
        if candidate.is_file():
            with candidate.open(encoding="utf-8") as handle:
                return yaml.safe_load(handle) or {}
    return None


def load_policy_files(
    config: AppConfig | None = None,
) -> tuple[list[PolicyRule], list[str], dict[str, Any], ProtectedConfig]:
    """Load cleanup rules, protective priority, default policy and protected patterns.

    Falls back to the built-in conservative defaults when the YAML files are absent so the
    classifier always works, while honouring user edits when the files exist.
    """
    cleanup = _load_yaml(config, "cleanup_rules.yaml") or {}
    raw_rules = cleanup.get("rules") or DEFAULT_RULES
    priority = [str(item) for item in (cleanup.get("priority") or DEFAULT_PRIORITY)]
    default_policy = {**DEFAULT_POLICY, **(cleanup.get("default") or {})}
    rules: list[PolicyRule] = []
    for raw in raw_rules:
        if "condition" not in raw:
            # A rule with no known condition can never fire; skip it rather than guessing.
            continue
        rules.append(
            PolicyRule(
                id=str(raw["id"]),
                condition=str(raw["condition"]),
                classification=str(raw["classification"]),
                confidence=float(raw.get("confidence", 0.5)),
                reason_codes=list(raw.get("reason_codes", [])),
                explanation=str(raw.get("explanation", "")),
                requires_manual_approval=bool(raw.get("requires_manual_approval", True)),
                requires_canonical=bool(raw.get("requires_canonical", False)),
                params=dict(raw.get("params", {})),
            )
        )

    protected = _load_yaml(config, "protected_patterns.yaml") or {}
    protected_config = ProtectedConfig(
        suffixes=frozenset(protected.get("extensions", []) or []) | PROTECTED_SUFFIXES,
        filenames=frozenset(protected.get("filenames", []) or [])
        | {"id_rsa", "id_dsa", "credentials", "passwords"},
        directory_names=frozenset(protected.get("directory_names", []) or []) | {".git", ".ssh"},
    )
    return rules, priority, default_policy, protected_config


# --- Condition predicates ---------------------------------------------------------------
# Each predicate receives the per-entry fact dictionary and returns a boolean.  Keeping the
# logic in code (rather than an on-drive expression language) is a deliberate safety choice.

def _cond_analysis_or_scan_failed(facts: dict[str, Any]) -> bool:
    return bool(facts["scan_status"] == "ERROR" or facts["analysis_failed"])


def _cond_protected_signal(facts: dict[str, Any]) -> bool:
    protected: ProtectedConfig = facts["protected_config"]
    if facts["suffix"] in protected.suffixes or facts["name"] in protected.filenames:
        return True
    components = set(facts["relative_path"].split("/")[:-1])
    return bool(components & protected.directory_names)


def _cond_exact_duplicate_noncanonical(facts: dict[str, Any]) -> bool:
    return bool(
        facts["canonical_entry_id"]
        and facts["canonical_entry_id"] != facts["entry_id"]
        and facts["group_size"] >= 2
    )


def _cond_office_temporary_lock(facts: dict[str, Any]) -> bool:
    name = facts["name"]
    if not name.startswith("~$"):
        return False
    if facts["size_bytes"] > 65_536:
        return False
    if facts["suffix"] not in {".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx"}:
        return False
    return bool(facts["has_office_lock_sibling"])


def _cond_python_bytecode_cache(facts: dict[str, Any]) -> bool:
    rel = facts["relative_path"]
    return "/__pycache__/" in f"/{rel}" and facts["suffix"] == ".pyc" and facts["in_project"]


def _cond_virtualenv_with_reproducibility(facts: dict[str, Any]) -> bool:
    return bool(facts["virtualenv_project_root"] is not None and facts["project_has_spec"])


def _cond_node_modules_with_lockfile(facts: dict[str, Any]) -> bool:
    return bool(facts["node_modules_project_root"] is not None and facts["project_has_lockfile"])


def _cond_old_duplicate_installer(facts: dict[str, Any]) -> bool:
    if facts["suffix"] not in _INSTALLER_SUFFIXES:
        return False
    if facts["group_size"] < 2:
        return False
    modified = facts["modified_at"]
    return bool(modified is not None and (facts["now"] - modified) > _INSTALLER_AGE_SECONDS)


CONDITIONS: dict[str, Callable[[dict[str, Any]], bool]] = {
    "analysis_or_scan_failed": _cond_analysis_or_scan_failed,
    "protected_signal": _cond_protected_signal,
    "exact_duplicate_noncanonical": _cond_exact_duplicate_noncanonical,
    "office_temporary_lock": _cond_office_temporary_lock,
    "python_bytecode_cache": _cond_python_bytecode_cache,
    "virtualenv_with_reproducibility": _cond_virtualenv_with_reproducibility,
    "node_modules_with_lockfile": _cond_node_modules_with_lockfile,
    "old_duplicate_installer": _cond_old_duplicate_installer,
}


def evaluate_rule(rule: PolicyRule, facts: dict[str, Any]) -> ClassificationResult | None:
    """Return a classification if the rule's condition matches this entry, else ``None``."""
    predicate = CONDITIONS.get(rule.condition)
    if predicate is None or not predicate(facts):
        return None
    canonical = facts["canonical_entry_id"] if rule.requires_canonical else None
    return ClassificationResult(
        entry_id=facts["entry_id"],
        classification=rule.classification,
        confidence=rule.confidence,
        primary_reason_code=rule.reason_codes[0] if rule.reason_codes else rule.id.upper(),
        reason_codes=list(rule.reason_codes),
        rule_ids=[rule.id],
        explanation=rule.explanation,
        canonical_entry_id=canonical,
        requires_manual_approval=rule.requires_manual_approval,
    )


def resolve_rule_conflicts(
    results: list[ClassificationResult], priority: list[str]
) -> ClassificationResult:
    """Pick the most protective classification; a lower-safety rule never overrides it."""
    rank = {name: index for index, name in enumerate(priority)}

    def sort_key(result: ClassificationResult) -> tuple[int, float]:
        return (rank.get(result.classification, len(priority)), -result.confidence)

    winner = min(results, key=sort_key)
    # Preserve the audit trail of every rule that matched, most protective first.
    ordered = sorted(results, key=sort_key)
    all_rule_ids = [rid for result in ordered for rid in result.rule_ids]
    return ClassificationResult(
        entry_id=winner.entry_id,
        classification=winner.classification,
        confidence=winner.confidence,
        primary_reason_code=winner.primary_reason_code,
        reason_codes=winner.reason_codes,
        rule_ids=all_rule_ids,
        explanation=winner.explanation,
        canonical_entry_id=winner.canonical_entry_id,
        requires_manual_approval=winner.requires_manual_approval,
    )


def evaluate_all_rules(
    rules: list[PolicyRule],
    facts: dict[str, Any],
    priority: list[str],
    default_policy: dict[str, Any],
) -> ClassificationResult:
    matches = [result for rule in rules if (result := evaluate_rule(rule, facts)) is not None]
    if not matches:
        return ClassificationResult(
            entry_id=facts["entry_id"],
            classification=str(default_policy["classification"]),
            confidence=float(default_policy.get("confidence", 0.5)),
            primary_reason_code=(default_policy.get("reason_codes") or ["KEEP_BY_DEFAULT"])[0],
            reason_codes=list(default_policy.get("reason_codes", [])),
            rule_ids=[],
            explanation=str(default_policy.get("explanation", "")),
            canonical_entry_id=None,
            requires_manual_approval=bool(default_policy.get("requires_manual_approval", False)),
        )
    return resolve_rule_conflicts(matches, priority)


def _project_marker_dirs(database: Database) -> tuple[set[str], set[str]]:
    """Directories (source-root-relative) that contain a dependency spec / lockfile."""
    spec_dirs: set[str] = set()
    lock_dirs: set[str] = set()
    markers = _DEPENDENCY_SPECS | _LOCKFILES
    placeholders = ",".join("?" for _ in markers)
    for row in database.iter_rows(
        f"SELECT relative_path,name FROM filesystem_entries WHERE entry_type='file' AND name IN ({placeholders})",
        tuple(markers),
    ):
        rel = str(row["relative_path"]).replace("\\", "/")
        parent = rel.rsplit("/", 1)[0] if "/" in rel else ""
        if row["name"] in _DEPENDENCY_SPECS:
            spec_dirs.add(parent)
        if row["name"] in _LOCKFILES:
            lock_dirs.add(parent)
    return spec_dirs, lock_dirs


def _segment_root_before(relative_path: str, segment: str) -> str | None:
    """Return the project-root prefix immediately before ``/<segment>/`` if present."""
    needle = f"/{segment}/"
    padded = f"/{relative_path}"
    index = padded.find(needle)
    if index < 0:
        return None
    return padded[1:index]  # drop the leading slash we added


def _in_project(relative_path: str, spec_dirs: set[str]) -> bool:
    parts = relative_path.split("/")
    for cut in range(len(parts)):
        prefix = "/".join(parts[:cut])
        if prefix in spec_dirs:
            return True
    return False


def _build_facts(
    row: Any,
    protected_config: ProtectedConfig,
    spec_dirs: set[str],
    lock_dirs: set[str],
    now: float,
    sibling_conn: Any,
) -> dict[str, Any]:
    rel = str(row["relative_path"]).replace("\\", "/")
    venv_root = next(
        (
            root
            for segment in (".venv", "venv", "env")
            if (root := _segment_root_before(rel, segment)) is not None
        ),
        None,
    )
    node_root = _segment_root_before(rel, "node_modules")
    facts: dict[str, Any] = {
        "entry_id": int(row["id"]),
        "name": str(row["name"]),
        "suffix": (row["suffix"] or "").lower(),
        "relative_path": rel,
        "scan_status": row["scan_status"],
        "analysis_failed": bool(row["analysis_failed"]),
        "size_bytes": int(row["size_bytes"] or 0),
        "modified_at": row["modified_at"],
        "canonical_entry_id": row["canonical_entry_id"],
        "group_size": int(row["group_size"] or 0),
        "protected_config": protected_config,
        "now": now,
        "virtualenv_project_root": venv_root,
        "node_modules_project_root": node_root,
        "project_has_spec": (venv_root in spec_dirs) if venv_root is not None else False,
        "project_has_lockfile": (node_root in lock_dirs) if node_root is not None else False,
        "in_project": _in_project(rel, spec_dirs),
        "has_office_lock_sibling": False,
    }
    if str(row["name"]).startswith("~$") and row["parent_entry_id"] is not None:
        sibling = sibling_conn.execute(
            "SELECT 1 FROM filesystem_entries WHERE parent_entry_id=? AND name=? AND entry_type='file'",
            (row["parent_entry_id"], str(row["name"])[2:]),
        ).fetchone()
        facts["has_office_lock_sibling"] = sibling is not None
    return facts


_CLASSIFY_SELECT = """SELECT e.id,e.name,e.suffix,e.relative_path,e.scan_status,e.size_bytes,e.modified_at,e.parent_entry_id,
    g.canonical_entry_id,
    (SELECT COUNT(*) FROM exact_duplicate_members m2 WHERE m2.group_id=g.id) AS group_size,
    EXISTS(SELECT 1 FROM entry_content_links l JOIN analysis_artifacts a ON a.content_object_id=l.content_object_id
           WHERE l.entry_id=e.id AND a.status IN ('ERROR','UNSUPPORTED')) AS analysis_failed
    FROM filesystem_entries e
    LEFT JOIN exact_duplicate_members m ON m.entry_id=e.id
    LEFT JOIN exact_duplicate_groups g ON g.id=m.group_id
    WHERE e.entry_type='file' AND e.id IN (%s)"""

_CLASSIFY_INSERT = (
    "INSERT OR REPLACE INTO classifications(entry_id,classification,confidence,primary_reason_code,"
    "reason_codes_json,rule_ids_json,explanation,canonical_entry_id,requires_manual_approval) "
    "VALUES(?,?,?,?,?,?,?,?,?)"
)


def classify_all_entries(
    database: Database, config: AppConfig, job_id: int | None = None, scope=None
) -> dict[str, int]:
    """Classify every file in the current inventory, recording full audit evidence.

    Streaming keeps memory bounded on million-entry inventories: rows are read from an
    independent read-only connection while classifications are written in bounded batches on
    the main connection, so no read cursor is invalidated by a concurrent write.

    "Every file" means every file *in scope*. Classifying all of history re-derived a verdict for
    every snapshot of every file ever scanned — cost that grows with how often you have run the
    tool rather than with the size of the drive, and a verdict for rows no current-state report
    should ever show.
    """
    from .analysers.scope import resolve_scope

    entry_sql, scope_params = resolve_scope(database, scope).entry_id_sql()
    rules, priority, default_policy, protected_config = load_policy_files(config)
    spec_dirs, lock_dirs = _project_marker_dirs(database)
    now = time.time()
    write_conn = database.connect()
    write_conn.execute(
        f"DELETE FROM classifications WHERE entry_id IN ({entry_sql})", scope_params
    )
    write_conn.commit()
    if job_id:
        total = database.fetch_one(
            f"SELECT COUNT(*) AS n FROM filesystem_entries WHERE entry_type='file' AND id IN ({entry_sql})",
            scope_params,
        )
        update_job(database, job_id, total_estimate=int(total["n"]) if total else 0)
    batch_size = max(1, int(config.section("performance")["batch_size"]))
    counts: dict[str, int] = {}
    batch: list[tuple[Any, ...]] = []
    processed = 0
    with database.read_connection() as read_conn:
        read_cursor = read_conn.execute(_CLASSIFY_SELECT % entry_sql, scope_params)
        while rows := read_cursor.fetchmany(batch_size):
            for row in rows:
                facts = _build_facts(row, protected_config, spec_dirs, lock_dirs, now, read_conn)
                result = evaluate_all_rules(rules, facts, priority, default_policy)
                counts[result.classification] = counts.get(result.classification, 0) + 1
                batch.append(result.to_row())
            write_conn.executemany(_CLASSIFY_INSERT, batch)
            write_conn.commit()
            processed += len(batch)
            checkpoint(database, job_id, processed_count=processed)
            batch.clear()
    return counts
