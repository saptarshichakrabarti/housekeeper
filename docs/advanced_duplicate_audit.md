# Advanced Duplicate Intelligence — Architecture Audit

This document is the mandated Phase 0 audit for the advanced duplicate / archival / collection
upgrade. It records the current identity, similarity, relationship, and canonical-copy
architecture, the schema changes required, and the staged implementation plan.

## 1. Currently supported identity and similarity methods

| Method | Where | Evidence strength |
|--------|-------|-------------------|
| Full SHA-256 (byte identity) | `hashing.compute_full_hash`, `content_objects` | Cryptographic, exact |
| Quick hash (size + head/mid/tail sample) | `hashing.compute_quick_hash` | Candidate generation only |
| Exact duplicate grouping | `analysers/exact_duplicates.py` | Tier-1 exact (byte identity via content objects) |
| Directory content overlap (verified hash sets) | `analysers/directory_overlap.py` | Exact set overlap of Tier-1 hashes |
| Backup lineage (shared child hashes) | `analysers/backup_lineage.py` | Heuristic, pairwise |
| Document version families (filename + text similarity) | `analysers/document_versions.py` | Probabilistic (SequenceMatcher) |
| Perceptual image similarity (8×8 average hash) | `analysers/images.py` | Probabilistic, review-only |
| Archive manifest hash (member path list) | `analysers/archives.py` | Structural, not equivalence |

**Gap:** "duplicate" is effectively binary — a file is either a byte-identical member of an
`exact_duplicate_group` or it is grouped by a single ad-hoc similarity relationship. There is
no explicit *evidence tier* and no format-aware normalized equivalence.

## 2. Current evidence model

Relationships are stored in a single generic `relationships` table:

```
id, source_type, source_id, target_type, target_id, relationship_type,
confidence, evidence_json, relationship_version, created_at
```

It has **no** `evidence_tier`, `algorithm`, `algorithm_version`,
`configuration_fingerprint`, `explanation`, `status`, or `invalidated_at`. Confidence and a
free-form `evidence_json` are the only qualifiers. `relationship_groups` /
`relationship_group_members` provide typed group entities (DOCUMENT_FAMILY, IMAGE_SIMILARITY).

## 3. Current relationship types (in use)

`EXACT_DUPLICATE_MEMBER`, `DUPLICATE_CONTENT`, `MOSTLY_CONTAINED_IN`,
`LIKELY_BACKUP_SUCCESSOR`, `LIKELY_VERSION_OF`, `VISUALLY_SIMILAR_TO`, `CONTAINS`
(project→directory). These are flat strings with no tier or algorithm provenance.

## 4. Current canonical-copy model

`exact_duplicate_groups.canonical_entry_id` holds a **single** canonical entry per group
(shortest path heuristic). `canonical_overrides` lets a review session override it. There are
no role-specific assignments (preservation master vs. editable source vs. access copy).

## 5. Existing backup-overlap implementation

`directory_overlap` builds `directory_summaries`, then (as of the last pass) generates
candidate directory pairs via an inverted index over verified content hashes and emits
`MOSTLY_CONTAINED_IN`. `backup_lineage` compares direct-child hash sets pairwise. Neither
computes *marginal preservation value* (unique bytes / unique families / unique protected
objects) or supports removal simulation.

## 6. Current limitations (relevant to this upgrade)

- No evidence tiers; a perceptual match and a cryptographic match share one schema.
- No format-aware normalization (Office repackaging, image metadata variants, PDF re-encoding
  are all invisible — they look like distinct byte content).
- No content-defined chunking / partial-overlap.
- No scalable near-duplicate document candidate generation (MinHash/LSH).
- Single canonical copy, not role-based preservation.
- No collection-level reasoning (events, work sessions, record series, preservation risk).
- No known-content registry or review-learning.

## 7. Schema changes required (this pass — additive, migration v4 → v5)

Implemented now (concrete responsibilities, no empty layers):

- `normalization_profiles` — versioned, fingerprinted normalization definitions with
  documented `loss_characteristics`.
- `normalized_content_artifacts` — per-(content object, profile) normalized hashes.
- `content_relationships` — the tiered relationship table (evidence tier, algorithm,
  algorithm version, configuration fingerprint, explanation, status, invalidated_at, canonical
  symmetric ordering).
- `similarity_signatures` — per-content signatures (image pHash/dHash, office structural hash,
  etc.), versioned and fingerprinted.
- `canonical_assignments` — role-based canonical assignments; existing single canonical is
  migrated to the `CANONICAL_LOCATION` role.

Deferred to later phases (documented, not stubbed as empty tables): `content_chunks`,
`chunk_occurrences`, `chunk_profiles`, `content_overlap_results`, `collection_clusters`,
`collection_members`, `record_series`, `retention_policies`, `preservation_assessments`,
`review_learning_models`, `review_learning_predictions`.

## 8. Optional dependencies

- Already available: Pillow (image decode / pixel + perceptual hashing), python-docx / openpyxl
  / python-pptx (Office structure), PyMuPDF (PDF), rapidfuzz.
- Deferred/optional and **not** required for core install: `datasketch`/`numpy` (MinHash can be
  implemented dependency-free), `tlsh`, `ssdeep`, a FastCDC package (a pure-Python CDC fallback
  is planned for correctness).

## 9. Computational cost estimates

- Normalization (this pass): one decode/parse per **content object** (reused across duplicate
  paths), bounded by existing content-analysis caching. Cheap relative to hashing.
- Chunking / MinHash / binary fuzzy (deferred): potentially large indexes — must be opt-in,
  scoped, and estimated before running (Phase 3–4 requirement).

## 10. Backward-compatibility risks

- Bumping `SCHEMA_VERSION` 4 → 5: existing tests asserting `== 4` must be updated; the fresh-DB
  and legacy-migration paths must still complete. Mitigated: v5 migration is additive only.
- Existing `relationships`, `exact_duplicate_groups`, `canonical_overrides` are **untouched**;
  new tiered relationships live in `content_relationships`, so existing exact-duplicate
  behavior, reports, and the graph are unchanged.

## 11. Migration plan (v4 → v5)

1. Create the five new tables via `CREATE TABLE IF NOT EXISTS`.
2. Backfill `canonical_assignments` with `CANONICAL_LOCATION` for every existing
   `exact_duplicate_groups.canonical_entry_id` (migration step 3 of the prompt).
3. Do **not** run normalization, chunking, or signature generation during migration.
4. Preserve existing decisions; do not silently reclassify old relationship rows.
5. Record schema version 5.

## 12. Staged implementation plan

| Phase | Scope | Status this session |
|-------|-------|---------------------|
| 0 | Audit + baseline | ✅ this document; 136 tests green baseline |
| 1 | Evidence tiers, relationship schema, normalization profiles, versioning/invalidation, migration | ✅ implemented + tested |
| 2 | Office / image / archive normalized equivalence + tiered relationships | ✅ image + Office + archive equivalence implemented + tested |
| 7 (partial) | Canonical roles table + CANONICAL_LOCATION migration + basic role assignment | ✅ implemented + tested |
| 3 | Document MinHash + LSH | ⏳ deferred (design fixed; dependency-free plan) |
| 4 | Content-defined chunking | ⏳ deferred (opt-in, scoped, estimate-first) |
| 5 | Archive-vs-directory + cross-format derivation | ⏳ deferred |
| 6 | Backup marginal value + removal simulation | ⏳ deferred |
| 8–13 | Record series, lifecycle, events, preservation risk, priority, fuzzy binary, active learning, dashboard/graph views | ⏳ deferred |

The safety ordering mandated by the prompt is respected: evidence tiers, normalization
profiles, and schema migrations land first; no chunking or fuzzy similarity is introduced yet.
