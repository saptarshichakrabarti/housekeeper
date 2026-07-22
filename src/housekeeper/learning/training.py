"""Train and evaluate a small logistic-regression model over historical review decisions."""

from __future__ import annotations

import json

from .features import FEATURE_NAMES, entry_features

MODEL_VERSION = "1"
FEATURE_SCHEMA_VERSION = "1"
_POSITIVE = {"APPROVE_FOR_REVIEW"}
_NEGATIVE = {"MARK_KEEP", "REJECT_RECOMMENDATION", "MARK_PROTECTED"}

_LABELLED_QUERY = """
SELECT e.id, c.classification, c.confidence, c.canonical_entry_id, c.requires_manual_approval,
       e.suffix, e.size_bytes, d.decision
FROM review_decisions d
JOIN filesystem_entries e ON d.target_type='ENTRY' AND d.target_id=e.id
LEFT JOIN classifications c ON c.entry_id=e.id
WHERE d.current=1 AND d.stale=0
"""


def _dataset(database, allow_protected: bool):
    rows = database.fetch_all(_LABELLED_QUERY)
    features, labels = [], []
    for row in rows:
        decision = row["decision"]
        if decision in _POSITIVE:
            label = 1
        elif decision in _NEGATIVE:
            label = 0
        else:
            continue
        if not allow_protected and (row["classification"] in ("PROTECTED", "ERROR")):
            continue
        features.append(entry_features(row))
        labels.append(label)
    return features, labels


def _fit(features, labels):
    import numpy as np

    x = np.asarray(features, dtype=float)
    y = np.asarray(labels, dtype=float)
    means = x.mean(axis=0)
    stds = x.std(axis=0)
    stds[stds == 0] = 1.0
    xs = (x - means) / stds
    weights = np.zeros(xs.shape[1])
    bias = 0.0
    learning_rate = 0.1
    for _ in range(800):
        prediction = 1.0 / (1.0 + np.exp(-(xs @ weights + bias)))
        error = prediction - y
        weights -= learning_rate * (xs.T @ error / len(y) + 0.01 * weights)
        bias -= learning_rate * error.mean()
    return weights.tolist(), float(bias), means.tolist(), stds.tolist()


def train_model(database, config) -> dict:
    section = config.section("learning")
    minimum = int(section["minimum_training_examples"])
    features, labels = _dataset(database, bool(section.get("allow_protected_categories", False)))
    if len(labels) < minimum or len(set(labels)) < 2:
        return {"status": "insufficient_training_data", "examples": len(labels), "minimum": minimum}
    weights, bias, means, stds = _fit(features, labels)
    metrics = _metrics(features, labels, weights, bias, means, stds)
    artifact = {
        "weights": weights,
        "bias": bias,
        "means": means,
        "stds": stds,
        "feature_names": FEATURE_NAMES,
    }
    database.connect().execute("UPDATE review_learning_models SET active=0")
    database.connect().execute(
        """INSERT INTO review_learning_models(model_type,model_version,feature_schema_version,training_scope_json,training_count,metrics_json,artifact_json,active)
           VALUES('logistic_regression',?,?,?,?,?,?,1)""",
        (
            MODEL_VERSION,
            FEATURE_SCHEMA_VERSION,
            json.dumps({"current_nonstale_decisions": True}),
            len(labels),
            json.dumps(metrics),
            json.dumps(artifact),
        ),
    )
    database.connect().commit()
    return {"status": "trained", "examples": len(labels), **metrics}


def _predict_proba(features, weights, bias, means, stds):
    import numpy as np

    x = (np.asarray(features, dtype=float) - np.asarray(means)) / np.asarray(stds)
    return 1.0 / (1.0 + np.exp(-(x @ np.asarray(weights) + bias)))


def _metrics(features, labels, weights, bias, means, stds) -> dict:
    import numpy as np

    proba = _predict_proba(features, weights, bias, means, stds)
    predicted = (proba >= 0.5).astype(int)
    actual = np.asarray(labels)
    tp = int(((predicted == 1) & (actual == 1)).sum())
    fp = int(((predicted == 1) & (actual == 0)).sum())
    fn = int(((predicted == 0) & (actual == 1)).sum())
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    return {
        "precision": round(precision, 3),
        "recall": round(recall, 3),
        "accuracy": round(float((predicted == actual).mean()), 3),
        "positives": int(actual.sum()),
    }


def evaluate_model(database, config) -> dict:
    row = database.fetch_one(
        "SELECT metrics_json,training_count FROM review_learning_models WHERE active=1 ORDER BY id DESC LIMIT 1"
    )
    if not row:
        return {"status": "no_active_model"}
    return {"status": "ok", "training_count": row["training_count"], **json.loads(row["metrics_json"])}
