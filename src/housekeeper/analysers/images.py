from pathlib import Path
from collections import defaultdict
from .scope import analyserScope, scoped_entry_ids
from ..jobs import check_cancelled, update_job


def calculate_hash_distance(a: str, b: str) -> int:
    return sum(x != y for x, y in zip(a, b)) + abs(len(a) - len(b))


def extract_image_metadata(path: Path, config):
    try:
        from PIL import Image

        with Image.open(path) as im:
            if im.width * im.height > config.section("images")["max_pixels"]:
                return {"analysis_status": "ERROR", "analysis_error": "pixel limit"}
            thumb = im.convert("L").resize((8, 8))
            # An 8-bit grayscale ("L") image yields exactly one byte per pixel; tobytes avoids
            # the deprecated getdata() while staying valid across Pillow versions.
            values = list(thumb.tobytes())
            average = sum(values) / len(values)
            phash = "".join("1" if value >= average else "0" for value in values)
            return {
                "format": im.format,
                "width": im.width,
                "height": im.height,
                "perceptual_hash": phash,
                "analysis_status": "OK",
            }
    except Exception as exc:
        return {"analysis_status": "ERROR", "analysis_error": str(exc)}


def create_thumbnail(path: Path, content_object_id: int, config) -> str | None:
    """Create a bounded local thumbnail; originals are never modified or exposed."""
    if not config.section("content_store").get("store_image_thumbnails", True):
        return None
    try:
        from PIL import Image

        output_dir = config.workspace / ".housekeeper" / "thumbnails"
        output_dir.mkdir(parents=True, exist_ok=True)
        output = output_dir / f"{content_object_id}.jpg"
        maximum = int(config.section("content_store").get("thumbnail_max_dimension", 512))
        with Image.open(path) as image:
            image.thumbnail((maximum, maximum))
            image.convert("RGB").save(output, "JPEG", quality=82, optimize=True)
        return str(output)
    except Exception:
        return None


def run_image_analysis(
    database, config, scope: analyserScope | None = None, job_id: int | None = None
):
    import json
    from ..relationships import replace_relationship_group, upsert_relationship

    rows = database.fetch_all(
        "SELECT id,content_object_id,artifact_json FROM analysis_artifacts WHERE analyser_name='images' AND status='COMPLETED'"
    )
    if scope:
        entry_ids = scoped_entry_ids(database, scope)
        linked = []
        if not entry_ids:
            rows = []
        else:
            placeholders = ",".join("?" for _ in entry_ids)
            linked = database.fetch_all(
                "SELECT DISTINCT content_object_id FROM entry_content_links WHERE entry_id IN ("
                + placeholders
                + ")",
                tuple(sorted(entry_ids)),
            )
        allowed_content = {int(row["content_object_id"]) for row in linked}
        rows = [row for row in rows if int(row["content_object_id"]) in allowed_content]
    # A perceptual hash prefix is a bounded candidate funnel.  It avoids comparing every
    # photo with every other photo while preserving a conservative relationship threshold.
    buckets = defaultdict(list)
    for row in rows:
        artifact = json.loads(row["artifact_json"] or "{}")
        perceptual_hash = artifact.get("perceptual_hash")
        if perceptual_hash:
            buckets[perceptual_hash[:8]].append((row, artifact))
    for index, (key, bucket) in enumerate(buckets.items(), start=1):
        if job_id:
            check_cancelled(database, job_id)
        group_members: set[int] = set()
        for i, (left, a) in enumerate(bucket):
            for right, b in bucket[i + 1 :]:
                if not a.get("perceptual_hash") or not b.get("perceptual_hash"):
                    continue
                distance = calculate_hash_distance(a["perceptual_hash"], b["perceptual_hash"])
                if distance <= 8:
                    group_members.update(
                        (int(left["content_object_id"]), int(right["content_object_id"]))
                    )
                    upsert_relationship(
                        database,
                        "CONTENT_OBJECT",
                        left["content_object_id"],
                        "CONTENT_OBJECT",
                        right["content_object_id"],
                        "VISUALLY_SIMILAR_TO",
                        1 - distance / 64,
                        {"perceptual_distance": distance},
                        "1",
                    )
        if len(group_members) > 1:
            replace_relationship_group(
                database,
                "IMAGE_SIMILARITY",
                key,
                sorted(group_members),
                {"perceptual_hash_prefix": key, "threshold": 8},
                "2",
            )
        if job_id:
            update_job(
                database,
                job_id,
                "RUNNING",
                processed_count=index,
                checkpoint={"last_hash_prefix": key},
            )
