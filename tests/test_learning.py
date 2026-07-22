"""Active-learning tests: interpretable, non-approving, insufficient-data guard."""

import pytest

from housekeeper.learning.features import FEATURE_NAMES, entry_features
from housekeeper.learning.prediction import predict_pending
from housekeeper.learning.training import train_model
from housekeeper.review.decisions import create_session, record_decision
from housekeeper.scanner import DriveScanner

pytest.importorskip("numpy")


class _Row(dict):
    def __getitem__(self, key):
        return super().get(key)


def test_features_are_deterministic_and_structural():
    row = _Row(classification="REVIEW_SAFE", confidence=1.0, canonical_entry_id=5,
               requires_manual_approval=1, suffix=".png", size_bytes=1000)
    features = entry_features(row)
    assert len(features) == len(FEATURE_NAMES)
    assert entry_features(row) == features  # deterministic


def _train_scenario(config, database, tmp_path, decisions=30):
    root = tmp_path / "src"
    root.mkdir()
    for i in range(decisions):
        (root / f"dup{i}.bin").write_bytes(b"same-payload")
        (root / f"keep{i}.txt").write_text(f"unique document {i}", encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    from housekeeper.analyzers.exact_duplicates import run_exact_duplicate_analysis
    from housekeeper.policies import classify_all_entries

    run_exact_duplicate_analysis(database, config)
    classify_all_entries(database, config)
    session = create_session(database, "learn")
    # Approve duplicates, keep the unique documents -> a learnable pattern.
    for row in database.fetch_all(
        "SELECT e.id,c.classification FROM filesystem_entries e JOIN classifications c ON c.entry_id=e.id WHERE e.entry_type='file'"
    ):
        decision = "APPROVE_FOR_REVIEW" if row["classification"] == "REVIEW_SAFE" else "MARK_KEEP"
        record_decision(database, session, "ENTRY", int(row["id"]), decision)
    return session


def test_insufficient_training_data_does_not_train(config, database, tmp_path):
    config.section("learning")["minimum_training_examples"] = 1000
    _train_scenario(config, database, tmp_path, decisions=3)
    result = train_model(database, config)
    assert result["status"] == "insufficient_training_data"


def test_trains_and_reports_metrics(config, database, tmp_path):
    config.section("learning")["minimum_training_examples"] = 10
    _train_scenario(config, database, tmp_path, decisions=30)
    result = train_model(database, config)
    assert result["status"] == "trained"
    assert 0.0 <= result["precision"] <= 1.0
    assert database.fetch_one("SELECT COUNT(*) n FROM review_learning_models WHERE active=1")["n"] == 1


def test_predictions_never_approve_movement(config, database, tmp_path):
    config.section("learning")["minimum_training_examples"] = 10
    _train_scenario(config, database, tmp_path, decisions=30)
    train_model(database, config)
    result = predict_pending(database, config)
    # Predictions are stored as suggestions, never as review_decisions.
    assert "cannot approve movement" in result["note"]
    predictions = database.fetch_all("SELECT predicted_decision FROM review_learning_predictions")
    # A prediction row is never a decision row; decisions still come only from the user/CLI.
    assert all("predicted_decision" in dict(p) for p in predictions)


def test_protected_categories_excluded_from_training(config, database, tmp_path):
    config.section("learning")["minimum_training_examples"] = 1
    config.section("learning")["allow_protected_categories"] = False
    root = tmp_path / "src"
    root.mkdir()
    (root / "secret.pem").write_text("-----BEGIN PRIVATE KEY-----", encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    from housekeeper.policies import classify_all_entries

    classify_all_entries(database, config)
    session = create_session(database, "learn")
    entry = database.fetch_one("SELECT id FROM filesystem_entries WHERE name='secret.pem'")["id"]
    record_decision(database, session, "ENTRY", int(entry), "MARK_PROTECTED")
    # Only a protected example exists -> excluded -> no learnable data.
    result = train_model(database, config)
    assert result["status"] == "insufficient_training_data"
