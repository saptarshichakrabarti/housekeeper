"""Policy-engine tests: rule matching, conservative conflict resolution, fail-closed."""

import time


from housekeeper.constants import Classification
from housekeeper.policies import (
    ClassificationResult,
    ProtectedConfig,
    classify_all_entries,
    evaluate_all_rules,
    evaluate_rule,
    load_policy_files,
    resolve_rule_conflicts,
)
from tests.conftest import analyse_and_classify


def _protected() -> ProtectedConfig:
    return ProtectedConfig(
        suffixes=frozenset({".pem", ".key"}),
        filenames=frozenset({"id_rsa"}),
        directory_names=frozenset({".git", ".ssh"}),
    )


def make_facts(**overrides):
    base = {
        "entry_id": 1,
        "name": "file.txt",
        "suffix": ".txt",
        "relative_path": "dir/file.txt",
        "scan_status": "OK",
        "analysis_failed": False,
        "size_bytes": 10,
        "modified_at": None,
        "canonical_entry_id": None,
        "group_size": 0,
        "protected_config": _protected(),
        "now": time.time(),
        "virtualenv_project_root": None,
        "node_modules_project_root": None,
        "project_has_spec": False,
        "project_has_lockfile": False,
        "in_project": False,
        "has_office_lock_sibling": False,
    }
    base.update(overrides)
    return base


def test_load_policy_files_defaults_and_priority():
    rules, priority, default, protected = load_policy_files(None)
    ids = {rule.id for rule in rules}
    assert "parser-or-filesystem-error" in ids
    assert "exact-duplicate-noncanonical" in ids
    assert priority[0] == "ERROR"
    assert priority[-1] == "REVIEW_SAFE"
    assert default["classification"] == "KEEP"
    assert ".pem" in protected.suffixes


def test_error_condition_fails_closed():
    rules, priority, default, _ = load_policy_files(None)
    facts = make_facts(analysis_failed=True)
    result = evaluate_all_rules(rules, facts, priority, default)
    assert result.classification == Classification.ERROR
    assert result.classification != Classification.REVIEW_SAFE


def test_protected_beats_review_safe_conservative_resolution():
    # A python cache file that also lives under a protected .git directory: PROTECTED wins.
    rules, priority, default, _ = load_policy_files(None)
    facts = make_facts(
        name="cache.pyc",
        suffix=".pyc",
        relative_path=".git/hooks/__pycache__/cache.pyc",
        in_project=True,
    )
    result = evaluate_all_rules(rules, facts, priority, default)
    assert result.classification == Classification.PROTECTED


def test_resolve_rule_conflicts_keeps_full_audit_trail():
    protective = ClassificationResult(1, "PROTECTED", 0.9, "P", ["P"], ["r-protected"], "", None, True)
    weak = ClassificationResult(1, "REVIEW_SAFE", 1.0, "S", ["S"], ["r-safe"], "", 2, True)
    winner = resolve_rule_conflicts([weak, protective], load_policy_files(None)[1])
    assert winner.classification == "PROTECTED"
    assert set(winner.rule_ids) == {"r-protected", "r-safe"}


def test_default_keep_when_no_rule_matches():
    rules, priority, default, _ = load_policy_files(None)
    result = evaluate_all_rules(rules, make_facts(), priority, default)
    assert result.classification == Classification.KEEP
    assert result.requires_manual_approval is False


def test_duplicate_rule_requires_noncanonical_and_group():
    rules, priority, default, _ = load_policy_files(None)
    duplicate_rule = next(r for r in rules if r.id == "exact-duplicate-noncanonical")
    # canonical member itself must not be flagged
    assert evaluate_rule(duplicate_rule, make_facts(entry_id=5, canonical_entry_id=5, group_size=2)) is None
    result = evaluate_rule(duplicate_rule, make_facts(entry_id=6, canonical_entry_id=5, group_size=2))
    assert result is not None
    assert result.canonical_entry_id == 5
    assert "EXACT_SHA256_DUPLICATE" in result.reason_codes


def test_virtualenv_rule_needs_reproducibility():
    rules, priority, default, _ = load_policy_files(None)
    venv_rule = next(r for r in rules if r.id == "virtualenv-regenerable")
    assert evaluate_rule(venv_rule, make_facts(virtualenv_project_root="P", project_has_spec=False)) is None
    assert evaluate_rule(venv_rule, make_facts(virtualenv_project_root="P", project_has_spec=True)) is not None


def test_old_installer_requires_duplicate_not_only_age():
    rules, priority, default, _ = load_policy_files(None)
    installer_rule = next(r for r in rules if r.id == "old-duplicate-installer")
    ancient = time.time() - 2 * 365 * 24 * 3600
    # Old but unique -> age alone is insufficient.
    assert evaluate_rule(installer_rule, make_facts(suffix=".exe", modified_at=ancient, group_size=1)) is None
    assert evaluate_rule(installer_rule, make_facts(suffix=".exe", modified_at=ancient, group_size=2)) is not None


def test_classify_integration_over_fixture(scanned):
    database, config, _ = scanned
    counts = analyse_and_classify(database, config)
    assert counts  # non-empty distribution
    rows = {
        r["relative_path"]: (r["classification"], r["primary_reason_code"])
        for r in database.fetch_all(
            "SELECT e.relative_path,c.classification,c.primary_reason_code FROM classifications c JOIN filesystem_entries e ON e.id=c.entry_id"
        )
    }
    assert rows["Project/.git/config"][0] == "PROTECTED"
    assert rows["Secrets/id_rsa"][0] == "PROTECTED"
    assert rows["Project/__pycache__/main.pyc"][0] == "REVIEW_SAFE"
    # A duplicate is REVIEW_SAFE; a parser failure is ERROR, never REVIEW_SAFE.
    assert any(v[0] == "REVIEW_SAFE" and v[1] == "EXACT_SHA256_DUPLICATE" for v in rows.values())
    for classification, _reason in rows.values():
        assert classification in {c.value for c in Classification}


def test_classification_is_deterministic(scanned):
    database, config, _ = scanned
    first = analyse_and_classify(database, config)
    second = classify_all_entries(database, config)
    assert first == second
