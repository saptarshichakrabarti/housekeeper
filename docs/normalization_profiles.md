# Normalization Profiles

A normalization profile turns a file into a deterministic, versioned fingerprint that ignores a
**documented** set of non-content differences. A normalized-hash match implies Tier-2/3
equivalence, never byte identity — the raw SHA-256 always remains authoritative.

Profiles are persisted in `normalization_profiles` (name, algorithm, algorithm version,
configuration fingerprint, `loss_characteristics`). Results are stored per content object in
`normalized_content_artifacts`.

## Implemented profiles

### `IMAGE_PIXEL_EQUIVALENCE`
- **Detects:** identical decoded pixels regardless of container/encoding.
- **Discards (`loss_characteristics`):** container encoding, lossless recompression, metadata,
  EXIF, ICC profile.
- **Also computes:** an EXIF-orientation-normalized pixel hash (drives `ORIENTATION_VARIANT`),
  dimensions, mode, and EXIF *presence*.
- **Does not prove:** visual equivalence of *edited* images; a perceptual match is a separate,
  weaker Tier-5 signal.

### `OFFICE_PACKAGE_EQUIVALENCE` (DOCX / XLSX / PPTX)
- **Detects:** identical package member **content** (multiset of `member path → SHA-256`),
  excluding volatile property parts (`docProps/core.xml`, `docProps/app.xml`,
  `docProps/custom.xml`).
- **Discards:** ZIP member ordering, ZIP timestamps, compression method, document properties.
- **Preserves (so a difference still differs):** document text, formulas, tracked changes,
  comments, speaker notes, hidden sheets, embedded media, macros, custom XML.
- **Does not prove:** visual/textual equivalence when only extracted text matches — that would
  be textual similarity, a weaker tier.

### `ARCHIVE_CONTENT_EQUIVALENCE` (ZIP / TAR family)
- **Detects:** identical member content multiset, computed by **streaming** member content
  (archives are never extracted to disk).
- **Discards:** member ordering, member timestamps, compression method.
- **Bounded:** archives above `normalization.archives.max_content_bytes` (default 256 MiB) or
  above `archives.max_members` report `UNSUPPORTED` rather than doing unbounded work.

## Failure modes

- Missing optional parser (e.g. Pillow) → `UNSUPPORTED` (never a lower retention value).
- Malformed / corrupt input → `ERROR`, isolated to that content object (fail closed).
- Unstable/unreadable representative → skipped; a different linked path is tried first
  (representative-path fallback).

## Reproducibility & invalidation

Each profile has an `algorithm_version` and a configuration fingerprint. When either changes,
`content_relationships` written by the old version are marked `INVALIDATED` and recomputed. This
keeps every normalized match explainable and regenerable.

## Cost / storage

One parse per content object. Storage is a single artifact row + one signature row per
(content object, profile). Deferred profiles: PDF (page-text / structural), audio (payload /
tag), tabular (row-order-sensitive / -insensitive).
