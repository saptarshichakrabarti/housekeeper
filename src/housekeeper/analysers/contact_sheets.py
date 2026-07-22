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

import json
from pathlib import Path

from ..jobs import check_cancelled, update_job
from .scope import analyserScope, scoped_entry_ids

_BACKGROUND = (245, 245, 245)
_PADDING = 6


def contact_sheet_dir(config) -> Path:
    return config.workspace / ".housekeeper" / "contact_sheets"


def contact_sheet_path(config, group_id: int) -> Path:
    return contact_sheet_dir(config) / f"group_{group_id}.jpg"


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
        except Exception:  # noqa: BLE001 - a bad thumbnail is skipped, never fatal
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
    database, config, scope: analyserScope | None = None, job_id: int | None = None
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

    allowed_content: set[int] | None = None
    if scope:
        entry_ids = scoped_entry_ids(database, scope)
        allowed_content = set()
        if entry_ids:
            placeholders = ",".join("?" for _ in entry_ids)
            allowed_content = {
                int(row["content_object_id"])
                for row in database.fetch_all(
                    "SELECT DISTINCT content_object_id FROM entry_content_links "
                    "WHERE entry_id IN (" + placeholders + ")",
                    tuple(sorted(entry_ids)),
                )
            }

    groups = database.fetch_all(
        "SELECT id FROM relationship_groups WHERE group_type='IMAGE_SIMILARITY' ORDER BY id"
    )
    sheets_written = 0
    truncated_groups = 0
    skipped_groups = 0
    for index, group in enumerate(groups, start=1):
        if job_id:
            check_cancelled(database, job_id)
        group_id = int(group["id"])
        members = database.fetch_all(
            "SELECT content_object_id FROM relationship_group_members "
            "WHERE group_id=? ORDER BY content_object_id",
            (group_id,),
        )
        member_ids = [int(row["content_object_id"]) for row in members]
        if allowed_content is not None:
            member_ids = [cid for cid in member_ids if cid in allowed_content]
        if len(member_ids) > max_members:
            truncated_groups += 1
            member_ids = member_ids[:max_members]  # deterministic: lowest content ids first
        thumbnails = [path for cid in member_ids if (path := _thumbnail_path(database, cid))]
        output_path = contact_sheet_path(config, group_id)
        if generate_contact_sheet(thumbnails, output_path, columns, cell_pixels):
            sheets_written += 1
        else:
            skipped_groups += 1
        if job_id:
            update_job(
                database,
                job_id,
                "RUNNING",
                processed_count=index,
                checkpoint={"last_group_id": group_id},
            )
    return {
        "status": "ok",
        "groups": len(groups),
        "sheets_written": sheets_written,
        "skipped_groups": skipped_groups,
        "truncated_groups": truncated_groups,
    }
