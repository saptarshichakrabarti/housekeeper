"""Preservation-risk tests: risks increase caution, never become deletion candidates."""

from housekeeper.analyzers.preservation_risk import assess_entry, run_preservation_risk_analysis
from housekeeper.policies import classify_all_entries
from housekeeper.scanner import DriveScanner


class _Row(dict):
    def __getitem__(self, key):
        return super().get(key)


def _row(suffix, scan_status="OK", analysis_failed=0):
    return _Row(suffix=suffix, scan_status=scan_status, analysis_failed=analysis_failed)


def test_legacy_office_flagged_for_migration():
    assessment = assess_entry(_row(".doc"))
    assert assessment["format_risk"] != "none"
    assert assessment["recommended_action"] == "KEEP_AND_MIGRATE"


def test_encrypted_flagged_for_key_documentation():
    assessment = assess_entry(_row(".gpg"))
    assert assessment["encryption_risk"] == "high"
    assert assessment["recommended_action"] == "NEEDS_KEY_DOCUMENTATION"


def test_parser_failure_is_integrity_review():
    assessment = assess_entry(_row(".pdf", analysis_failed=1))
    assert assessment["recommended_action"] == "NEEDS_INTEGRITY_REVIEW"


def test_ordinary_file_has_no_preservation_risk():
    assert assess_entry(_row(".txt")) is None


def test_recommended_actions_are_never_deletion():
    for suffix in (".doc", ".gpg", ".iso", ".sqlite", ".pst"):
        action = assess_entry(_row(suffix))["recommended_action"]
        assert "DELETE" not in action and "REMOVE" not in action
        assert action.startswith(("KEEP", "NEEDS"))


def test_preservation_queue_separate_from_clutter(config, database, tmp_path):
    root = tmp_path / "src"
    root.mkdir()
    (root / "legacy.doc").write_bytes(b"\xd0\xcf\x11\xe0 legacy office")
    (root / "vault.gpg").write_bytes(b"encrypted")
    (root / "plain.txt").write_text("ordinary", encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    classify_all_entries(database, config)
    result = run_preservation_risk_analysis(database, config)
    assert result["assessed"] >= 2  # legacy.doc + vault.gpg
    # Preservation assessments do not turn anything into a review/removal candidate.
    for row in database.fetch_all(
        "SELECT recommended_action FROM preservation_assessments"
    ):
        assert row["recommended_action"].startswith(("KEEP", "NEEDS"))
