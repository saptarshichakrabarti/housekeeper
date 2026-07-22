# Format-Aware Equivalence

This is the operational summary of the format-aware equivalence analyzer
(`analyzers/normalized_content.py`). See `normalization_profiles.md` for what each profile
discards and `duplicate_taxonomy.md` for evidence tiers.

## How it runs

```
housekeeper analyze normalized-content        # all supported formats
housekeeper analyze image-equivalence         # aliases (same analysis, format-scoped by suffix)
housekeeper analyze office-equivalence
housekeeper analyze archive-equivalence
```

Pipeline: ensure every supported-suffix file has a verified content object → normalize each
content object once (representative-path fallback) → store artifacts + signatures → group by
normalized hash → emit tiered `content_relationships` → assign canonical roles.

## Format distinctions produced

| Format | Relationship | Tier | Evidence |
|--------|--------------|------|----------|
| Image | `PIXEL_IDENTICAL` | 2 | identical decoded pixels, different bytes |
| Image | `ORIENTATION_VARIANT` | 3 | identical only after EXIF orientation |
| DOCX/XLSX/PPTX | `OFFICE_PACKAGE_EQUIVALENT` | 2 | identical member-content multiset (volatile props excluded) |
| ZIP/TAR | `ARCHIVE_REPACKAGING_VARIANT` | 2 | identical member-content multiset, different packaging |

## Safety properties (tested)

- A normalized match is **never** an exact-duplicate group: a `PIXEL_IDENTICAL` /
  `OFFICE_PACKAGE_EQUIVALENT` pair leaves `exact_duplicate_groups` empty when the bytes differ.
- Meaningful differences are **not** equivalent: an Office document with different body text, an
  archive with different member content, and a resized image all produce **no** equivalence.
- Parser/limit failures are `ERROR`/`UNSUPPORTED`, never a lower retention value.
- Results are deterministic and reproducible; changing a profile version invalidates and
  regenerates its relationships.

## Deferred format equivalence

PDF (page-text + structural + embedded-image fingerprints), audio (tag vs. payload
fingerprints), tabular (row-order-sensitive / -insensitive), and cross-format derivation
(DOCX→PDF, directory→ZIP) are designed in the prompt and scheduled for later phases; they are
**not** emitted yet, so no weaker signal is silently promoted to equivalence.
