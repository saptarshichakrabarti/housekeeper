"""Image metadata extraction and perceptual-similarity grouping.

The descriptor is a 64-bit **DCT** hash, stored as 16 hex characters: the image is reduced to
32x32 grayscale, transformed, and the low-frequency 8x8 block is thresholded at its median. It
replaced an 8x8 average hash, which downsampled to 64 pixels and compared each to the mean — that
describes coarse brightness layout and nothing else, so it called two different photographs of the
same scene similar and missed the same photograph after a gamma change.

Banding selectivity was the visible symptom: with an average hash, ~7 bits per band left buckets
of ~156 members at 20,000 descriptors, because the bits were strongly correlated with each other.
DCT coefficients are close to decorrelated, which is the property the pigeonhole index needs to be
selective rather than merely complete.

Two things follow from the 64-bit width, unchanged by the descriptor swap:

* **Distance is one instruction.** ``(a ^ b).bit_count()`` replaces a character-by-character
  comparison of a 64-character string.
* **The candidate index is complete.** The hash is split into 9 bands (one of 8 bits, eight of 7);
  by the pigeonhole principle two hashes within Hamming distance 8 must agree exactly on at least
  one band, so an equality join over bands cannot miss a true match. The previous 8-bit *prefix*
  bucket could miss a pair differing in a single bit, and compared all pairs within each bucket.

**G3**: bands and distance are a candidate funnel. Every emitted relationship is still gated on the
exact distance, and a perceptual match is never an exact-duplicate claim.
"""

from __future__ import annotations

import math
import time
from itertools import combinations, groupby
from pathlib import Path
from statistics import median

from ..jobs import check_cancelled, checkpoint
from .scope import AnalyserScope, resolve_scope

#: Width of the descriptor, and the number of hex characters it is stored in.
PHASH_BITS = 64
PHASH_HEX_LENGTH = PHASH_BITS // 4
#: Hamming distance at which two images are called visually similar.
SIMILARITY_THRESHOLD = 8
#: m >= r + 1 bands makes the band index complete for radius r.
PHASH_BANDS = SIMILARITY_THRESHOLD + 1
#: 64 bits over 9 bands: one of 8, eight of 7.
_BAND_WIDTHS = (8,) + (7,) * (PHASH_BANDS - 1)


#: Bumped whenever the descriptor or the clustering changes, so the previous generation's output is
#: deleted instead of being mixed with the new one. Pairs went to 2 and groups to 3 with the DCT
#: descriptor: an average-hash distance and a DCT distance are not the same measurement.
SIMILARITY_PAIR_VERSION = "2"
SIMILARITY_GROUP_VERSION = "3"

#: The DCT is computed over this many samples per axis and reduced to the low-frequency block.
DCT_SIZE = 32
#: Side of the retained low-frequency block; DCT_BLOCK**2 must equal PHASH_BITS.
DCT_BLOCK = 8


def _dct_basis() -> tuple[tuple[float, ...], ...]:
    """``basis[k][n]`` for a DCT-II of length :data:`DCT_SIZE`, k < :data:`DCT_BLOCK`.

    Only the first ``DCT_BLOCK`` output coefficients are ever used, so only those rows exist. Built
    once at import: the table is 8x32 floats and rebuilding it per image dominated the transform.

    Deliberately pure Python rather than ``numpy.fft.dct``/``scipy``. The descriptor is persisted
    and compared across runs and machines, so it has to be bit-reproducible; two BLAS builds that
    disagree in the last mantissa bit would flip a threshold comparison and silently reclassify
    images. One arithmetic implementation, no optional acceleration.
    """
    return tuple(
        tuple(math.cos(math.pi * (2 * n + 1) * k / (2 * DCT_SIZE)) for n in range(DCT_SIZE))
        for k in range(DCT_BLOCK)
    )


_DCT_BASIS = _dct_basis()


def _rank_transform(samples: bytes) -> list[int]:
    """Replace each sample by its rank among all samples.

    This is what makes the descriptor invariant to *any* strictly monotone tone curve — exposure,
    gamma, contrast, a camera's film simulation — by construction rather than by luck: a monotone
    curve cannot reorder pixels, so it cannot change a single rank.

    It is not a refinement. Measured on the validation corpus in ``tests/test_images.py``, without
    it a gamma of 0.7 moved **20 of 64 bits** while the closest pair of genuinely different images
    sat at **8** — exactly the similarity threshold, so the descriptor was one JPEG artefact away
    from calling two unrelated photographs the same. With it, the worst benign transformation costs
    6 bits and the closest distinct pair is 22.

    Ties take consecutive ranks in position order. ``sorted`` is stable, so equal samples resolve
    identically every time, and a monotone curve keeps equal samples equal — the invariant holds.
    """
    order = sorted(range(len(samples)), key=lambda index: samples[index])
    ranks = [0] * len(samples)
    for rank, index in enumerate(order):
        ranks[index] = rank
    return ranks


def dct_phash(samples: bytes) -> int:
    """A 64-bit DCT descriptor from ``DCT_SIZE**2`` grayscale bytes, row-major.

    Separable: the 1D transform runs along rows and then along columns, and both times keeps only
    the first :data:`DCT_BLOCK` coefficients. Transforming the full 32x32 and discarding 94% of it
    costs ~65k multiply-adds per image against ~10k for this.

    The threshold is the median of the 63 coefficients *excluding* DC. DC is the image's mean
    brightness — an order of magnitude larger than everything else and a pure offset, so including
    it would drag the median. Its own bit is therefore always set; that is a constant, so it
    contributes nothing to any Hamming distance.
    """
    if len(samples) != DCT_SIZE * DCT_SIZE:
        raise ValueError(f"expected {DCT_SIZE * DCT_SIZE} grayscale samples, got {len(samples)}")
    ranked = _rank_transform(samples)
    # Rows: DCT_SIZE rows of DCT_SIZE samples -> DCT_SIZE rows of DCT_BLOCK coefficients.
    rows = []
    for y in range(DCT_SIZE):
        row = ranked[y * DCT_SIZE : (y + 1) * DCT_SIZE]
        rows.append([math.fsum(row[n] * basis[n] for n in range(DCT_SIZE)) for basis in _DCT_BASIS])
    # Columns: same again over the partially transformed rows.
    block = [
        [math.fsum(rows[n][x] * basis[n] for n in range(DCT_SIZE)) for x in range(DCT_BLOCK)]
        for basis in _DCT_BASIS
    ]
    coefficients = [value for row in block for value in row]
    threshold = median(coefficients[1:])  # drop DC
    descriptor = 0
    for value in coefficients:
        descriptor = (descriptor << 1) | (1 if value > threshold else 0)
    return descriptor


def hash_distance(a: int, b: int) -> int:
    """Hamming distance between two descriptors."""
    return (a ^ b).bit_count()


def phash_bands(value: int) -> list[int]:
    """The banded projection of a descriptor, low bits first."""
    bands, offset = [], 0
    for width in _BAND_WIDTHS:
        bands.append((value >> offset) & ((1 << width) - 1))
        offset += width
    return bands


def parse_phash(value: object) -> int | None:
    """A stored descriptor as an integer, or None if it is not one of ours.

    Artifacts written before the integer descriptor hold a 64-character bit string, which is also
    valid hex and would parse into a meaningless number. The length check rejects them; their
    analyser version is superseded, so they are re-parsed rather than reinterpreted.
    """
    if not isinstance(value, str) or len(value) != PHASH_HEX_LENGTH:
        return None
    try:
        return int(value, 16)
    except ValueError:
        return None


def _capture_time(image) -> float | None:
    """EXIF capture time, read while the image is already open. Never GPS."""
    try:
        exif = getattr(image, "_getexif", lambda: None)() or {}
        stamp = exif.get(36867) or exif.get(306)  # DateTimeOriginal / DateTime
        if stamp:
            return time.mktime(time.strptime(str(stamp), "%Y:%m:%d %H:%M:%S"))
    except Exception:  # noqa: BLE001 - EXIF is best-effort; callers fall back to file time
        return None
    return None


def extract_image_metadata(path: Path, config):
    try:
        from PIL import Image

        with Image.open(path) as im:
            if im.width * im.height > config.section("images")["max_pixels"]:
                return {"analysis_status": "ERROR", "analysis_error": "pixel limit"}
            # Read here, once, rather than re-opening every photograph on every clustering run.
            capture_time = _capture_time(im)
            result = {
                "format": im.format,
                "width": im.width,
                "height": im.height,
                "capture_time": capture_time,
                "analysis_status": "OK",
            }
            if not config.section("images").get("enable_perceptual_hashing", True):
                # No descriptor means no band rows and no similarity candidates: the analyser
                # reports dimensions and capture time only.
                return result
            # BOX rather than the default resample filter: it is a plain area average, so the
            # samples do not depend on which Pillow version picked which reconstruction kernel.
            # The descriptor is persisted and compared across runs, so reproducibility outweighs
            # the marginal quality of a fancier filter.
            thumb = im.convert("L").resize((DCT_SIZE, DCT_SIZE), Image.Resampling.BOX)
            # An 8-bit grayscale ("L") image yields exactly one byte per pixel; tobytes avoids
            # the deprecated getdata() while staying valid across Pillow versions.
            phash = dct_phash(thumb.tobytes())
            result["perceptual_hash"] = f"{phash:0{PHASH_HEX_LENGTH}x}"
            return result
    except Exception as exc:  # noqa: BLE001 - any decoder failure becomes an ERROR artifact
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
    except Exception:  # noqa: BLE001 - a thumbnail is a convenience, never required
        return None


#: The newest COMPLETED images artifact for a content object. There can be several, one per
#: (analyser version, config fingerprint); only the latest describes the current descriptor.
_LATEST_ARTIFACT = """a.id=(SELECT MAX(x.id) FROM analysis_artifacts x
        WHERE x.content_object_id=a.content_object_id AND x.analyser_name='images'
          AND x.status='COMPLETED')"""


def refresh_phash_index(database, scope) -> int:
    """Bring the band index in step with the artifacts in scope. Returns rows reindexed.

    An anti-join on the stored descriptor: an unchanged corpus reindexes nothing.
    """
    content_sql, params = scope.content_object_id_sql()
    pending = database.reader().fetch_all(
        f"""SELECT a.content_object_id AS cid,
              json_extract(a.artifact_json,'$.perceptual_hash') AS phash
            FROM analysis_artifacts a
            LEFT JOIN image_phash_bands b
              ON b.content_object_id=a.content_object_id AND b.band_index=0
            WHERE a.analyser_name='images' AND a.status='COMPLETED'
              AND a.content_object_id IN ({content_sql})
              AND {_LATEST_ARTIFACT}
              AND json_extract(a.artifact_json,'$.perceptual_hash') IS NOT NULL
              AND (b.phash IS NULL
                   OR b.phash <> json_extract(a.artifact_json,'$.perceptual_hash'))""",
        params,
    )
    rows: list[tuple[int, int, int, str]] = []
    for row in pending:
        value = parse_phash(row["phash"])
        if value is None:
            continue
        cid = int(row["cid"])
        rows.extend(
            (cid, index, band, row["phash"]) for index, band in enumerate(phash_bands(value))
        )
    if rows:
        database.connect().executemany(
            """INSERT INTO image_phash_bands(content_object_id,band_index,band_value,phash)
               VALUES(?,?,?,?) ON CONFLICT(content_object_id,band_index)
               DO UPDATE SET band_value=excluded.band_value,phash=excluded.phash""",
            rows,
        )
        # Committed before the bucket scan, which reads on an independent read-only connection.
        database.connect().commit()
    return len(rows) // PHASH_BANDS


def confirmed_pairs(database, scope) -> tuple[dict[tuple[int, int], int], dict[int, str]]:
    """Every pair within the threshold, plus the descriptor of each object involved.

    Candidates come from the band buckets and are verified against the exact distance here. The
    buckets are streamed and compared in Python rather than joined in SQL: a self-join emits one
    row per shared band per candidate pair, which on a 20,000-descriptor corpus is 13.7M rows to
    hand back and de-duplicate. Scanning the 180,000 band rows in bucket order and comparing in
    place produces the same answer — measured 68 s against 4.9 s.

    Comparing per *distinct descriptor* rather than per object was tried here and reverted: it does
    cut the comparison count (1.9M to 0.1M on a corpus of 5,000 objects sharing 100 descriptors),
    but comparisons are not the cost. Emitting k(k-1)/2 pairs for k identical images is, and that
    is inherent to pairwise output. Measured 1.2x faster at best and 0.7x at worst.
    See docs/performance.md.
    """
    content_sql, params = scope.content_object_id_sql()
    rows = database.reader().iter_rows(
        f"""SELECT band_index,band_value,content_object_id,phash FROM image_phash_bands
            WHERE content_object_id IN ({content_sql})
            ORDER BY band_index,band_value,content_object_id""",
        params,
    )
    confirmed: dict[tuple[int, int], int] = {}
    phash_of: dict[int, str] = {}
    for _, bucket in groupby(rows, key=lambda row: (row["band_index"], row["band_value"])):
        members = [
            (int(row["content_object_id"]), value, row["phash"])
            for row in bucket
            if (value := parse_phash(row["phash"])) is not None
        ]
        if len(members) < 2:
            continue
        for (a_id, a_value, a_text), (b_id, b_value, b_text) in combinations(members, 2):
            distance = hash_distance(a_value, b_value)
            if distance <= SIMILARITY_THRESHOLD:
                confirmed[(a_id, b_id)] = distance
                phash_of[a_id], phash_of[b_id] = a_text, b_text
    return confirmed, phash_of


def _components(pairs: list[tuple[int, int]]) -> dict[int, list[int]]:
    """Connected components of the confirmed-similarity graph, keyed by their lowest member."""
    parent: dict[int, int] = {}

    def find(node: int) -> int:
        parent.setdefault(node, node)
        while parent[node] != node:
            parent[node] = parent[parent[node]]
            node = parent[node]
        return node

    for left, right in pairs:
        a, b = find(left), find(right)
        if a != b:
            parent[max(a, b)] = min(a, b)
    groups: dict[int, list[int]] = {}
    for node in parent:
        groups.setdefault(find(node), []).append(node)
    return {root: sorted(members) for root, members in groups.items()}


def run_image_analysis(
    database, config, scope: AnalyserScope | None = None, job_id: int | None = None
):
    from ..relationships import (
        invalidate_relationships,
        replace_relationship_group,
        upsert_relationship,
    )

    scope = resolve_scope(database, scope)
    refresh_phash_index(database, scope)
    # Anything produced by an earlier descriptor or grouping scheme is deleted rather than left
    # alongside the new output. Versioned, not shape-sniffed: the previous check compared key length,
    # which caught the 8-character prefix keys but *not* the average-hash groups — those key on 16
    # hex characters exactly as DCT groups do, so a length test silently kept them. Nothing joins to
    # a group id by foreign key and no user decision attaches to a perceptual group, so this loses
    # no evidence.
    database.connect().execute(
        "DELETE FROM relationship_groups WHERE group_type='IMAGE_SIMILARITY' "
        "AND (relationship_version<>? OR length(group_key)<>?)",
        (SIMILARITY_GROUP_VERSION, PHASH_HEX_LENGTH),
    )
    invalidate_relationships(
        database, "VISUALLY_SIMILAR_TO", except_version=SIMILARITY_PAIR_VERSION
    )

    confirmed, phash_of = confirmed_pairs(database, scope)
    # Sorted, so the relationships are written in the same order for the same bytes (G5).
    for (a_id, b_id), distance in sorted(confirmed.items()):
        upsert_relationship(
            database,
            "CONTENT_OBJECT",
            a_id,
            "CONTENT_OBJECT",
            b_id,
            "VISUALLY_SIMILAR_TO",
            1 - distance / PHASH_BITS,
            {"perceptual_distance": distance},
            SIMILARITY_PAIR_VERSION,
        )

    components = _components(sorted(confirmed))
    for index, (root, members) in enumerate(sorted(components.items()), start=1):
        if job_id:
            check_cancelled(database, job_id)
        # Keyed by the lowest member's descriptor: content-derived, so the same cluster keeps its
        # key across databases. Two components cannot share one — equal descriptors are distance 0
        # and therefore always in the same component.
        key = phash_of[root]
        replace_relationship_group(
            database,
            "IMAGE_SIMILARITY",
            key,
            members,
            {"threshold": SIMILARITY_THRESHOLD, "bands": PHASH_BANDS, "member_count": len(members)},
            SIMILARITY_GROUP_VERSION,
        )
        checkpoint(database, job_id, processed_count=index, state={"last_group": key})
    # This analyser is a stage: the write primitives no longer commit per row, so the one
    # commit that makes its work durable belongs here.
    database.connect().commit()
