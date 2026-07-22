# Advanced Duplicate Intelligence & Collection Management

Operational reference for the advanced analysers added on schema v6. Every method states what
it detects, what it does **not** prove, its cost, and its evidence tier. None of them ever
deletes or auto-moves a file; the strongest action remains manifest-approved movement to review.

## Content-defined chunking — `analyse chunks` / `analyse chunk-overlap`  (Tier 4)

- **Detects:** partial content overlap between large files (shared byte regions) using a
  pure-Python FastCDC gear-hash chunker. Emits `PARTIAL_CONTENT_OVERLAP` / `NEAR_SUBSET_CONTENT`.
- **Does not prove:** byte identity — distinct content objects always have distinct SHA-256.
- **Scalability:** opt-in (`chunking.enabled`, `minimum_file_size_bytes` default 128 MiB);
  candidate pairs come from an inverted chunk index with stop-chunk suppression (never all-pairs);
  `chunks estimate` reports cost first; `derived-data clear CHUNK_INDEX` removes only derived data.
- **Cost/storage:** ~1 hash per chunk (avg 64 KiB); index size is reported by `chunks estimate`.
- **Full reference:** `docs/content_defined_chunking.md`.

## Document near-duplicates — `analyse document-minhash`  (Tier 5)

- **Detects:** near-duplicate prose via word shingles → MinHash → LSH candidates, then **exact
  shingle-Jaccard verification**. Emits `NEAR_DUPLICATE_DOCUMENT` / `TEXTUALLY_SIMILAR`.
- **Does not prove:** anything on its own — MinHash only generates candidates; a relationship
  requires exact verification ≥ `verification_threshold`. Shared boilerplate/templates are
  rejected by verification, never merged into a version family.
- **Cost:** dependency-free; one signature per document content object.
- **Full reference:** `docs/document_similarity.md`.

## Format equivalence — `analyse normalized-content`  (Tiers 2–3)

Image pixel/orientation, Office package, archive content (see `format_equivalence.md`), plus
**PDF page-text equivalence** (`PDF_TEXT_EQUIVALENT`, Tier 3): identical extracted text after
re-encoding. PDFs with no extractable text report `UNSUPPORTED` (never match on empty text). Text
equivalence is never a claim of visual identity.

## Cross-format derivation — `analyse derivations`  (Tier 6)

- **Detects:** editable→export relationships (DOCX/PPTX/MD → PDF/HTML) from matching normalized
  stem + same directory + timestamp proximity. Emits `LIKELY_EXPORT`.
- **Does not prove:** direction with certainty; confidence < 1.0, review-only. Default guidance:
  keep the editable source and the final export.

## Backup marginal value — `analyse backup-value`, `collections simulate-removal`

- **Computes** each collection's *unique* contribution (unique bytes, content objects, protected
  items), not raw size, and a value class (`HIGH_MARGINAL_VALUE` …
  `FULLY_CONTENT_REDUNDANT_CONTEXT_REMAINS`). `simulate-removal` reports what would lose all
  copies — **simulation only, nothing moves**.

## Record series — `analyse record-series`

Conservative rule-based functional categories (installers, source, photos, financial, …) with
confidence; ambiguous items default to `UNKNOWN` (review). Advisory personal categories, not
legal retention determinations.

## Preservation risk — `analyse preservation-risk`, `preservation queue`

Flags legacy/encrypted/parser-failed/unknown-container formats with migration/documentation/
integrity actions. A preservation risk **increases caution** and never becomes a deletion
candidate; the queue is separate from clutter review.

## Review prioritization & lifecycle — `analyse review-priority` / `analyse lifecycle`

Risk-adjusted ranking with explicit, configurable component scores (`review_priority.weights`)
and stored explanations; categories `QUICK_SAFE_WIN` … `PRESERVATION_FIRST` (preservation risk
always dominates). Lifecycle assigns advisory `ACTIVE/ARCHIVE/COLD_ARCHIVE/MANUAL_REVIEW/
PROTECTED/DEFERRED` states — nothing is reorganized.

## Photo events — `analyse photo-events`

Time-gap clustering over EXIF capture time (fallback to file mtime). Precise GPS is never read or
stored.

## Known-content registry — `known assert` / `known list`

Auditable local assertions (`KNOWN_REGENERABLE`, `KNOWN_INSTALLER`, …) surfaced during review as
advisory signals. A global public hash list never auto-authorizes movement.

## Active learning — `learning train` / `evaluate` / `predict` / `disable`

Interpretable logistic regression over prior review decisions (no raw document text as a
feature). Guards: stays disabled below `learning.minimum_training_examples`; excludes protected
categories from training; predictions are **suggestions only** and can never approve movement or
alter canonical roles.

## Graph — content-equivalence / partial-overlap / derivation-family projections

Bounded, aggregated projections over `content_relationships` so tiered relationships are visible
in the graph, subject to the same node/edge hard limits.

## Safety summary

No permanent deletion; no similarity-only movement approval; parser/limit failures raise caution;
preservation failures never lower retention value; predictions never feed manifest approval; all
new relationships are additive and never reclassify existing exact-duplicate behavior.
