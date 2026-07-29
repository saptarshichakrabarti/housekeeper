"""Space treemap: per-folder size plus the two reclaimable aggregates, one level per request.

The aggregates are the part worth testing (the squarify layout is exercised through its own unit
check below, on the same algorithm the page ships). Two things must hold: duplicate bytes count only
the *redundant* copies — never the canonical one, or a folder holding the single kept copy would look
reclaimable — and the drill-down stays lazy, one level at a time.
"""

from __future__ import annotations

import pytest

from housekeeper.analysers.exact_duplicates import run_exact_duplicate_analysis
from housekeeper.graph.explorer import build_treemap
from housekeeper.policies import classify_all_entries
from housekeeper.scanner import DriveScanner

PAYLOAD = "duplicated payload " * 64  # big enough that the bytes are visible in the sums


@pytest.fixture
def drive(config, database, tmp_path):
    root = tmp_path / "drive"
    (root / "Originals" / "nested").mkdir(parents=True)
    (root / "Backup").mkdir()
    (root / "Originals" / "nested" / "photo.txt").write_text(PAYLOAD, encoding="utf-8")
    (root / "Backup" / "photo.txt").write_text(PAYLOAD, encoding="utf-8")
    (root / "Originals" / "unique.txt").write_text("one of a kind", encoding="utf-8")
    DriveScanner(database, config).scan(root, incremental=False)
    run_exact_duplicate_analysis(database, config)
    classify_all_entries(database, config)
    return root


def _by_name(payload):
    return {child["name"]: child for child in payload["children"]}


def test_roots_are_one_tile_per_source(drive, database):
    payload = build_treemap(database)
    assert payload["node"] is None
    assert [child["kind"] for child in payload["children"]] == ["SOURCE_ROOT"]
    root_tile = payload["children"][0]
    assert root_tile["size_bytes"] > 0
    assert root_tile["expandable"] is True


def test_directory_tiles_carry_recursive_size_and_reclaimable_bytes(drive, database):
    source = build_treemap(database)["children"][0]
    children = _by_name(build_treemap(database, source["node"]))
    assert set(children) == {"Originals", "Backup"}
    # Originals holds a file two levels down, so its size must be recursive, not "files right here".
    assert children["Originals"]["size_bytes"] > len(PAYLOAD)
    assert children["Originals"]["expandable"] is True
    # Exactly one of the two copies is redundant, so exactly one folder reports duplicate bytes.
    duplicate_totals = {name: child["duplicate_bytes"] for name, child in children.items()}
    assert sorted(duplicate_totals.values()) == [0, len(PAYLOAD)]


def test_drill_down_returns_one_level_only(drive, database):
    source = build_treemap(database)["children"][0]
    originals = _by_name(build_treemap(database, source["node"]))["Originals"]
    level = build_treemap(database, originals["node"])
    assert set(_by_name(level)) == {"nested", "unique.txt"}
    assert level["label"] == "Originals"
    file_tile = _by_name(level)["unique.txt"]
    assert file_tile["kind"] == "FILE"
    assert file_tile["expandable"] is False
    assert file_tile["duplicate_bytes"] == 0


def test_reviewable_bytes_follow_the_classifier(drive, database):
    source = build_treemap(database)["children"][0]
    children = _by_name(build_treemap(database, source["node"]))
    reviewable = sum(child["reviewable_bytes"] for child in children.values())
    classified = database.fetch_one(
        "SELECT COALESCE(SUM(e.size_bytes),0) b FROM current_classifications c "
        "JOIN current_entries e ON e.id=c.entry_id WHERE c.classification LIKE 'REVIEW_%'"
    )["b"]
    assert reviewable == classified


def test_a_bad_node_is_rejected(drive, database):
    with pytest.raises(ValueError, match="node must look like"):
        build_treemap(database, "ENTRY:../../etc/passwd")
    with pytest.raises(ValueError, match="unknown or non-expandable"):
        build_treemap(database, "ENTRY:999999")


def test_treemap_page_and_endpoint_render(drive, database, config):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from housekeeper.dashboard.app import create_app

    client = TestClient(create_app(database, config=config))
    page = client.get("/treemap").text
    assert "treemap.js" in page
    assert "reclaimable share" in page
    # The same numbers are available as a table, so the view is never colour-only.
    assert "The same numbers as a table" in page
    payload = client.get("/api/treemap/children").json()
    assert payload["children"] and payload["children"][0]["size_bytes"] > 0
    assert client.get("/api/treemap/children?node=nonsense").status_code == 422


# The layout the page ships, re-implemented in Python for one assertion: areas proportional, no
# overlap. It is the property that makes a treemap readable, and it is worth a test that does not
# need a browser.
def _squarify(values, x, y, width, height):
    rest = [v for v in values if v > 0]
    total = sum(rest)
    if not rest or total <= 0:
        return []
    out, area, scale = [], (x, y, width, height), (width * height) / total

    def worst(row, side):
        row_sum = sum(row)
        return max(side * side * max(row) / (row_sum**2), row_sum**2 / (side * side * min(row)))

    while rest:
        ax, ay, aw, ah = area
        side = min(aw, ah)
        row: list[float] = []
        while rest:
            candidate = [*row, rest[0] * scale]
            if row and worst(candidate, side) > worst(row, side):
                break
            row = candidate
            rest = rest[1:]
        thickness = sum(row) / side
        offset = 0.0
        horizontal = aw >= ah
        for value in row:
            length = value / thickness
            out.append(
                (ax, ay + offset, thickness, length)
                if horizontal
                else (ax + offset, ay, length, thickness)
            )
            offset += length
        area = (ax + thickness, ay, aw - thickness, ah) if horizontal else (ax, ay + thickness, aw, ah - thickness)
        if area[2] <= 0 or area[3] <= 0 or not rest:
            break
        scale = (area[2] * area[3]) / sum(rest)
    return out


def test_squarify_areas_are_proportional_and_tiles_never_overlap():
    values = [600, 300, 100, 40, 10]
    boxes = _squarify(values, 0, 0, 400, 250)
    assert len(boxes) == len(values)
    total_value, total_area = sum(values), 400 * 250
    for value, (_x, _y, w, h) in zip(values, boxes, strict=True):
        assert w * h == pytest.approx(total_area * value / total_value, rel=1e-6)
    for i, (ax, ay, aw, ah) in enumerate(boxes):
        for bx, by, bw, bh in boxes[i + 1 :]:
            overlap_x = min(ax + aw, bx + bw) - max(ax, bx)
            overlap_y = min(ay + ah, by + bh) - max(ay, by)
            assert overlap_x <= 1e-9 or overlap_y <= 1e-9


def test_reclaimable_share_is_a_union_not_a_sum(drive, database):
    """A redundant copy is usually also classified REVIEW_*; counting it twice invents bytes.

    The bug this pins produced a share above 100% (silently clamped) and made every folder holding
    duplicates look maximally reclaimable regardless of what else was in it.
    """
    source = build_treemap(database)["children"][0]
    children = _by_name(build_treemap(database, source["node"]))
    for name, child in children.items():
        assert child["reclaimable_bytes"] <= child["size_bytes"], name
        # Union, so never the sum of the two parts, and never less than either of them.
        assert child["reclaimable_bytes"] >= max(child["duplicate_bytes"], child["reviewable_bytes"])
        assert child["reclaimable_bytes"] <= child["duplicate_bytes"] + child["reviewable_bytes"]
    # Whichever side holds the redundant copy (the canonical is chosen by shortest path), that copy is
    # both redundant *and* classified REVIEW_*: its bytes count once, not twice.
    redundant = next(child for child in children.values() if child["duplicate_bytes"])
    assert redundant["duplicate_bytes"] == len(PAYLOAD)
    assert redundant["reviewable_bytes"] == len(PAYLOAD)
    assert redundant["reclaimable_bytes"] == len(PAYLOAD)
    assert redundant["reclaimable_bytes"] < redundant["duplicate_bytes"] + redundant["reviewable_bytes"]
