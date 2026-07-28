"""Deterministic contact-sheet (montage) generation for image-similarity groups.

A contact sheet composites the already-generated thumbnails of an ``IMAGE_SIMILARITY`` group into a
single bounded grid image, so a reviewer can eyeball a whole near-duplicate cluster at once.

Safety properties:

* Built **only from existing thumbnails** (small, already-decoded, size-bounded) — never from
  original files — so it cannot trigger a decompression bomb and never re-reads originals.
* **Deterministic**: members are sorted, the grid layout and neutral background are fixed, and no
  timestamps are embedded, so the same group always yields byte-comparable output.
* **Non-destructive**: writes derived JPEGs under the workspace and nothing else; regenerating is
  always safe and the sheets can be cleared without touching source data or review decisions.

Opt-in via ``images.create_contact_sheets``. Capability-gated on Pillow: if Pillow is missing it
reports unavailable and produces nothing (never an error).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..jobs import check_cancelled, checkpoint
from .scope import AnalyserScope, resolve_scope

_BACKGROUND = (245, 245, 245)
_PADDING = 6


def contact_sheet_dir(config) -> Path:
    return config.workspace / ".housekeeper" / "contact_sheets"


def contact_sheet_path(config, group_id: int) -> Path:
    return contact_sheet_dir(config) / f"group_{group_id}.jpg"


def _input_key(member_ids: list[int], thumbnails: list[str], columns: int, cell_pixels: int) -> str:
    """Everything the rendered sheet depends on. Same key ⇒ same bytes, so skip the render."""
    stamps = [(t, Path(t).stat().st_mtime_ns, Path(t).stat().st_size) for t in thumbnails]
    payload = json.dumps([member_ids, stamps, columns, cell_pixels], sort_keys=True)
    return hashlib.sha256(payload.encode()).hexdigest()


def _stored_key(database, group_id: int) -> str | None:
    row = database.fetch_one(
        "SELECT input_key FROM contact_sheet_renders WHERE group_id=?", (group_id,)
    )
    return str(row["input_key"]) if row else None


def _record_key(database, group_id: int, key: str) -> None:
    database.connect().execute(
        "INSERT INTO contact_sheet_renders(group_id,input_key,rendered_at) VALUES(?,?,CURRENT_TIMESTAMP) "
        "ON CONFLICT(group_id) DO UPDATE SET input_key=excluded.input_key,rendered_at=CURRENT_TIMESTAMP",
        (group_id, key),
    )


def _forget_key(database, group_id: int) -> None:
    database.connect().execute("DELETE FROM contact_sheet_renders WHERE group_id=?", (group_id,))


def _pillow_available() -> bool:
    try:
        import PIL.Image  # noqa: F401

        return True
    except ImportError:
        return False


def _thumbnail_path(database, content_object_id: int) -> str | None:
    """The recorded thumbnail path for a content object, if one exists on disk."""
    row = database.fetch_one(
        "SELECT artifact_json FROM analysis_artifacts "
        "WHERE analyser_name='images' AND content_object_id=? AND status='COMPLETED'",
        (content_object_id,),
    )
    if not row:
        return None
    thumbnail = json.loads(row["artifact_json"] or "{}").get("thumbnail_path")
    if thumbnail and Path(thumbnail).is_file():
        return thumbnail
    return None


def generate_contact_sheet(
    thumbnail_paths: list[str], output_path: Path, columns: int, cell_pixels: int
) -> bool:
    """Composite thumbnails into a fixed grid. Returns True on success, False if nothing usable."""
    from PIL import Image

    usable = []
    for thumbnail in thumbnail_paths:
        try:
            opened = Image.open(thumbnail)
            opened.load()  # force decode now so a corrupt thumbnail fails here, isolated per file
            usable.append(opened.convert("RGB"))
        except Exception:  # noqa: BLE001,S112 - a bad thumbnail is skipped, never fatal
            continue
    if len(usable) < 2:
        for member in usable:
            member.close()
        return False
    columns = max(1, columns)
    rows = (len(usable) + columns - 1) // columns
    step = cell_pixels + _PADDING
    canvas = Image.new(
        "RGB", (_PADDING + columns * step, _PADDING + rows * step), _BACKGROUND
    )
    for index, member in enumerate(usable):
        member.thumbnail((cell_pixels, cell_pixels))
        column, row = index % columns, index // columns
        # Centre each thumbnail within its fixed cell so a mixed-aspect group stays aligned.
        cell_x = _PADDING + column * step
        cell_y = _PADDING + row * step
        offset_x = cell_x + (cell_pixels - member.width) // 2
        offset_y = cell_y + (cell_pixels - member.height) // 2
        canvas.paste(member, (offset_x, offset_y))
        member.close()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(output_path, "JPEG", quality=82, optimize=True)
    return True


def run_contact_sheet_generation(
    database, config, scope: AnalyserScope | None = None, job_id: int | None = None
) -> dict:
    section = config.section("images")
    if not section.get("create_contact_sheets", True):
        return {"status": "disabled", "sheets_written": 0}
    if not _pillow_available():
        return {
            "status": "unavailable",
            "note": "install the images extra (Pillow) to render contact sheets",
            "sheets_written": 0,
        }
    columns = int(section.get("contact_sheet_columns", 4))
    cell_pixels = int(section.get("contact_sheet_cell_pixels", 160))
    max_members = int(section.get("contact_sheet_max_members", 36))

    content_sql, content_params = resolve_scope(database, scope).content_object_id_sql()

    groups = database.fetch_all(
        "SELECT id FROM relationship_groups WHERE group_type='IMAGE_SIMILARITY' ORDER BY id"
    )
    sheets_written = 0
    sheets_reused = 0
    truncated_groups = 0
    skipped_groups = 0
    # Reuse keys used to be `group_<id>.key` sidecars beside each sheet. They are rows now, so the
    # key is cascaded away when its group is replaced — a sidecar outliving its group authorised
    # reusing a sheet for a different set of members. Old sidecars are removed as they are found.
    for stale in contact_sheet_dir(config).glob("group_*.key"):
        stale.unlink(missing_ok=True)
    for index, group in enumerate(groups, start=1):
        if job_id:
            check_cancelled(database, job_id)
        group_id = int(group["id"])
        members = database.fetch_all(
            "SELECT content_object_id FROM relationship_group_members "
            f"WHERE group_id=? AND content_object_id IN ({content_sql}) ORDER BY content_object_id",
            (group_id, *content_params),
        )
        member_ids = [int(row["content_object_id"]) for row in members]
        if len(member_ids) > max_members:
            truncated_groups += 1
            member_ids = member_ids[:max_members]  # deterministic: lowest content ids first
        thumbnails = [path for cid in member_ids if (path := _thumbnail_path(database, cid))]
        output_path = contact_sheet_path(config, group_id)
        # The sheet is a pure function of its inputs, so unchanged membership and unchanged
        # thumbnails mean the render would reproduce the file already on disk.
        key = _input_key(member_ids, thumbnails, columns, cell_pixels)
        if output_path.is_file() and _stored_key(database, group_id) == key:
            sheets_reused += 1
        elif generate_contact_sheet(thumbnails, output_path, columns, cell_pixels):
            _record_key(database, group_id, key)
            sheets_written += 1
        else:
            _forget_key(database, group_id)
            skipped_groups += 1
        checkpoint(database, job_id, processed_count=index, state={"last_group_id": group_id})
    return {
        "status": "ok",
        "groups": len(groups),
        "sheets_written": sheets_written,
        "sheets_reused": sheets_reused,
        "skipped_groups": skipped_groups,
        "truncated_groups": truncated_groups,
    }
