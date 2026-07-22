"""Generate ranking suggestions from the active model. Never approves movement."""

from __future__ import annotations

import json

from .features import entry_features
from .training import _predict_proba

_PENDING_QUERY = """
SELECT e.id, c.classification, c.confidence, c.canonical_entry_id, c.requires_manual_approval,
       e.suffix, e.size_bytes
FROM filesystem_entries e
LEFT JOIN classifications c ON c.entry_id=e.id
WHERE e.entry_type='file'
  AND NOT EXISTS(SELECT 1 FROM review_decisions d WHERE d.target_type='ENTRY' AND d.target_id=e.id AND d.current=1)
"""


def predict_pending(database, config, limit: int = 5000) -> dict:
    model = database.fetch_one(
        "SELECT id,artifact_json FROM review_learning_models WHERE active=1 ORDER BY id DESC LIMIT 1"
    )
    if not model:
        return {"status": "no_active_model", "predictions": 0}
    artifact = json.loads(model["artifact_json"])
    weights, bias, means, stds = (
        artifact["weights"],
        artifact["bias"],
        artifact["means"],
        artifact["stds"],
    )
    conn = database.connect()
    conn.execute("DELETE FROM review_learning_predictions WHERE model_id=?", (model["id"],))
    written = 0
    for row in database.iter_rows(_PENDING_QUERY):
        if written >= limit:
            break
        probability = float(_predict_proba([entry_features(row)], weights, bias, means, stds)[0])
        decision = "APPROVE_FOR_REVIEW" if probability >= 0.5 else "MARK_KEEP"
        conn.execute(
            """INSERT OR REPLACE INTO review_learning_predictions(model_id,target_type,target_id,predicted_decision,probability,feature_summary_json)
               VALUES(?, 'ENTRY', ?, ?, ?, ?)""",
            (
                model["id"],
                int(row["id"]),
                decision,
                probability,
                json.dumps({"classification": row["classification"]}),
            ),
        )
        written += 1
    conn.commit()
    return {"status": "ok", "predictions": written, "note": "suggestions only; cannot approve movement"}
