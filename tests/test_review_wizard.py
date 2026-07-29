"""The duplicate-resolution wizard: rules propose, the existing decision flow records.

What must hold, in order of how badly it would hurt to get wrong:

1. The wizard adds no mutation capability — it writes ``review_decisions`` rows and nothing else.
2. Apply re-derives the keeper server-side, so a client cannot name the entries to approve.
3. A group whose members are not approvable (protected, unhashed) is skipped whole, never in part.
4. Keeper selection is deterministic, with the canonical choice as the tie-break.
"""

from __future__ import annotations

import pytest

from housekeeper.analysers.exact_duplicates import run_exact_duplicate_analysis
from housekeeper.canonical.roles import assign_canonical_roles
from housekeeper.policies import classify_all_entries
from housekeeper.review.decisions import create_session
from housekeeper.review.wizard import MAX_GROUPS_PER_REQUEST, apply_rule, preview
from housekeeper.scanner import DriveScanner

PAYLOADS = ("first shared payload", "second shared payload", "third shared payload")


@pytest.fixture
def duplicated(config, database, tmp_path):
    """Three groups, each with a copy under Originals/ and one under Backup/."""
    root = tmp_path / "drive"
    (root / "Originals").mkdir(parents=True)
    (root / "Backup").mkdir()
    for index, payload in enumerate(PAYLOADS):
        (root / "Originals" / f"file-{index}.txt").write_text(payload, encoding="utf-8")
        (root / "Backup" / f"file-{index}.txt").write_text(payload, encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    run_exact_duplicate_analysis(database, config)
    assign_canonical_roles(database)
    classify_all_entries(database, config)
    session = create_session(database, "wizard-session")
    return database, config, root, session


def _decisions(database, session):
    return {
        (row["target_id"], row["decision"])
        for row in database.fetch_all(
            "SELECT target_id,decision FROM review_decisions "
            "WHERE review_session_id=? AND current=1",
            (session,),
        )
    }


def test_keep_under_picks_the_copy_in_that_folder(duplicated):
    database, _config, _root, session = duplicated
    plan = preview(database, "keep-under", "Originals", session_id=session)
    assert plan["counts"]["groups"] == 3
    for group in plan["groups"]:
        assert group["keeper"]["relative_path"].startswith("Originals/")
        assert [m["relative_path"].split("/")[0] for m in group["approve"]] == ["Backup"]
    assert plan["counts"]["would_approve"] == 3


def test_keep_under_skips_groups_with_no_member_in_that_folder(duplicated):
    database, _config, _root, session = duplicated
    plan = preview(database, "keep-under", "Nowhere", session_id=session)
    assert plan["counts"]["actionable_groups"] == 0
    assert {group["skipped"] for group in plan["groups"]} == {"no member matches the rule"}
    # Nothing is proposed, so an apply writes nothing rather than falling back to some other copy.
    apply_rule(database, session, "keep-under", "Nowhere")
    assert _decisions(database, session) == set()


def test_keep_canonical_never_conflicts_with_the_canonical(duplicated):
    database, _config, _root, session = duplicated
    plan = preview(database, "keep-canonical", session_id=session)
    assert plan["counts"]["conflicts"] == 0
    canonicals = {
        int(row["canonical_entry_id"])
        for row in database.fetch_all(
            "SELECT canonical_entry_id FROM current_exact_duplicate_groups"
        )
    }
    assert {group["keeper"]["entry_id"] for group in plan["groups"]} == canonicals


def test_keep_newest_breaks_ties_on_the_canonical(duplicated):
    database, _config, _root, session = duplicated
    # The fixture copies share an mtime closely enough that ties are the normal case; whatever the
    # clock did, the keeper must be reproducible.
    first = preview(database, "keep-newest", session_id=session)
    second = preview(database, "keep-newest", session_id=session)
    assert [group["keeper"]["entry_id"] for group in first["groups"]] == [
        group["keeper"]["entry_id"] for group in second["groups"]
    ]
    for group in first["groups"]:
        newest = max(
            member["modified_at"] or 0 for member in group["approve"] + [group["keeper"]]
        )
        assert (group["keeper"]["modified_at"] or 0) == newest


def test_apply_writes_ordinary_decisions_and_nothing_else(duplicated):
    database, _config, root, session = duplicated
    before = sorted(path.name for path in root.rglob("*"))
    result = apply_rule(database, session, "keep-under", "Originals")
    assert result["kept"] == 3 and result["approved"] == 3
    decisions = _decisions(database, session)
    assert sum(1 for _, decision in decisions if decision == "APPROVE_FOR_REVIEW") == 3
    assert sum(1 for _, decision in decisions if decision == "MARK_KEEP") == 3
    # The source tree is untouched: the wizard records decisions, it does not move files.
    assert sorted(path.name for path in root.rglob("*")) == before
    assert database.fetch_one("SELECT COUNT(*) n FROM review_snapshots")["n"] == 0


def test_apply_is_idempotent(duplicated):
    database, _config, _root, session = duplicated
    apply_rule(database, session, "keep-under", "Originals")
    first = _decisions(database, session)
    apply_rule(database, session, "keep-under", "Originals")
    assert _decisions(database, session) == first
    # The superseded rows are history, not duplicates of the current decision.
    current = database.fetch_one(
        "SELECT COUNT(*) n FROM review_decisions WHERE review_session_id=? AND current=1",
        (session,),
    )["n"]
    assert current == len(first)


def test_a_protected_member_skips_its_whole_group(duplicated):
    database, _config, _root, session = duplicated
    victim = database.fetch_one(
        "SELECT e.id,m.group_id FROM current_entries e "
        "JOIN current_exact_duplicate_members m ON m.entry_id=e.id "
        "WHERE e.relative_path LIKE 'Backup/%' ORDER BY e.id LIMIT 1"
    )
    database.connect().execute(
        "UPDATE classifications SET classification='PROTECTED' WHERE entry_id=?", (victim["id"],)
    )
    database.connect().commit()
    plan = preview(database, "keep-under", "Originals", session_id=session)
    skipped = {group["group_id"]: group["skipped"] for group in plan["groups"]}
    assert "PROTECTED" in (skipped[int(victim["group_id"])] or "")
    assert plan["counts"]["actionable_groups"] == 2
    apply_rule(database, session, "keep-under", "Originals")
    # Neither the protected copy nor its group's keeper was decided: all or nothing per group.
    assert not any(target == victim["id"] for target, _ in _decisions(database, session))
    assert len(_decisions(database, session)) == 4  # two groups × (keeper + one approval)


def test_a_rescan_marks_the_wizards_decisions_stale_and_the_preview_says_so(duplicated):
    database, config, root, session = duplicated
    import os

    apply_rule(database, session, "keep-under", "Originals")
    # Touched, not rewritten: the copy is still part of its duplicate group, but the decision
    # recorded against the previous snapshot's row is no longer known to be current.
    touched = root / "Backup" / "file-0.txt"
    os.utime(touched, (touched.stat().st_atime + 120, touched.stat().st_mtime + 120))
    DriveScanner(database, config).scan(root, incremental=True)
    run_exact_duplicate_analysis(database, config)
    assert database.fetch_one(
        "SELECT COUNT(*) n FROM review_decisions WHERE review_session_id=? AND current=1 AND stale=1",
        (session,),
    )["n"] > 0
    plan = preview(database, "keep-under", "Originals", session_id=session)
    assert plan["counts"]["stale"] > 0


def test_unknown_rule_and_missing_prefix_are_refused(duplicated):
    database, _config, _root, session = duplicated
    with pytest.raises(ValueError, match="unknown rule"):
        preview(database, "keep-whatever", session_id=session)
    with pytest.raises(ValueError, match="path prefix"):
        preview(database, "keep-under", "", session_id=session)


def test_the_page_cap_is_enforced_and_continues_by_keyset(duplicated):
    database, _config, _root, session = duplicated
    with pytest.raises(ValueError, match="at most"):
        apply_rule(database, session, "keep-canonical", limit=MAX_GROUPS_PER_REQUEST + 1)
    page = preview(database, "keep-canonical", limit=2, session_id=session)
    assert page["counts"]["groups"] == 2
    assert page["next_after_id"] == page["groups"][-1]["group_id"]
    rest = preview(database, "keep-canonical", after_id=page["next_after_id"], limit=2)
    assert rest["counts"]["groups"] == 1
    assert rest["next_after_id"] is None


def test_apply_is_refused_when_the_groups_changed_since_the_preview(duplicated):
    """The confirmation is bound to the plan the user read, not to the rule alone.

    A rescan between preview and click gives every file a new entry row, so approvals would land on
    entries nobody looked at — and they would be fresh, non-stale decisions.
    """
    import os

    database, config, root, session = duplicated
    plan = preview(database, "keep-under", "Originals", session_id=session)
    touched = root / "Backup" / "file-0.txt"
    os.utime(touched, (touched.stat().st_atime + 120, touched.stat().st_mtime + 120))
    DriveScanner(database, config).scan(root, incremental=True)
    run_exact_duplicate_analysis(database, config)

    with pytest.raises(ValueError, match="out of date"):
        apply_rule(
            database, session, "keep-under", "Originals", expected_fingerprint=plan["fingerprint"]
        )
    assert _decisions(database, session) == set()
    # Previewing again produces a fingerprint that does apply.
    fresh = preview(database, "keep-under", "Originals", session_id=session)
    assert fresh["fingerprint"] != plan["fingerprint"]
    apply_rule(
        database, session, "keep-under", "Originals", expected_fingerprint=fresh["fingerprint"]
    )
    assert _decisions(database, session)


def test_the_fingerprint_covers_the_keeper_and_the_approvals(duplicated):
    database, _config, _root, session = duplicated
    under = preview(database, "keep-under", "Originals", session_id=session)
    canonical = preview(database, "keep-canonical", session_id=session)
    # Same groups, different keepers -> a preview of one rule can never confirm the other.
    assert under["fingerprint"] != canonical["fingerprint"]
    assert under["fingerprint"] == preview(database, "keep-under", "Originals", session_id=session)["fingerprint"]
    with pytest.raises(ValueError, match="out of date"):
        apply_rule(
            database, session, "keep-canonical", expected_fingerprint=under["fingerprint"]
        )


def test_endpoints_preview_apply_and_refuse_a_client_supplied_entry_list(duplicated):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from housekeeper.dashboard.app import create_app

    database, config, _root, session = duplicated
    client = TestClient(create_app(database, config=config))
    token = client.get("/api/csrf").json()["token"]

    page = client.get("/wizard").text
    assert "keep-canonical" in page and "nothing is moved or deleted" in page.lower()

    preview_json = client.get(
        f"/api/review/{session}/bulk/preview?rule=keep-under&path_prefix=Originals"
    ).json()
    assert preview_json["counts"]["actionable_groups"] == 3

    # No entry list is accepted: the only inputs are the rule, its scope, and the preview digest.
    fingerprint = preview_json["fingerprint"]
    assert client.post(
        f"/api/review/{session}/bulk?rule=keep-under&path_prefix=Originals"
        f"&fingerprint={fingerprint}&entry_ids=1",
        headers={"X-CSRF-Token": token},
    ).json()["approved"] == 3
    # The digest is required, and a wrong one is refused rather than applied.
    assert client.post(
        f"/api/review/{session}/bulk?rule=keep-canonical", headers={"X-CSRF-Token": token}
    ).status_code == 422
    assert client.post(
        f"/api/review/{session}/bulk?rule=keep-canonical&fingerprint={'0' * 64}",
        headers={"X-CSRF-Token": token},
    ).status_code == 422
    assert client.post(
        f"/api/review/{session}/bulk?rule=nonsense&fingerprint={fingerprint}",
        headers={"X-CSRF-Token": token},
    ).status_code == 422
    assert client.post(
        f"/api/review/{session}/bulk?rule=keep-canonical&fingerprint={fingerprint}"
    ).status_code == 403


def test_read_only_dashboard_previews_but_cannot_apply(duplicated):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from housekeeper.dashboard.app import create_app

    database, config, _root, session = duplicated
    viewer = TestClient(create_app(database, read_only=True, config=config))
    assert viewer.get(
        f"/api/review/{session}/bulk/preview?rule=keep-canonical"
    ).status_code == 200
    assert viewer.post(
        f"/api/review/{session}/bulk?rule=keep-canonical&fingerprint={'0' * 64}"
    ).status_code == 403
    assert "not applied" in viewer.get("/wizard").text
