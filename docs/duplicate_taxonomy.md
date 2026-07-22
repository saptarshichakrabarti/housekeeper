# Duplicate & Derivation Taxonomy

Housekeeper does not treat "duplicate" as a single binary property. Every relationship between
content carries an **evidence tier** that fixes how strong the claim is and what review policy
applies. Policy rules never collapse tiers: only Tier 1 is eligible for the strongest
exact-duplicate review classification.

## Evidence tiers

| Tier | Meaning | Example method |
|------|---------|----------------|
| `TIER_1_EXACT` | Cryptographically verified byte identity | SHA-256 of raw bytes (`content_objects`) |
| `TIER_2_NORMALIZED_EXACT` | Exact identity after a documented deterministic normalization | decoded-pixel hash, Office package member-content multiset, archive member-content multiset |
| `TIER_3_STRONG_EQUIVALENCE` | Strong format-aware evidence of equivalent user-visible content | EXIF-orientation-normalized pixel match |
| `TIER_4_PARTIAL_OVERLAP` | Verified shared subsets / chunks | content-defined chunk overlap *(deferred)* |
| `TIER_5_PROBABILISTIC_SIMILARITY` | Similarity model / fuzzy fingerprint | MinHash, perceptual hash, TLSH *(deferred / existing perceptual)* |
| `TIER_6_CONTEXTUAL_INFERENCE` | Derivation / lineage / semantic hypothesis | cross-format export inference *(deferred)* |

## Relationship types (implemented this pass)

- `BYTE_IDENTICAL` — Tier 1, the existing exact-duplicate group (unchanged).
- `PIXEL_IDENTICAL` — Tier 2: decoded pixels identical, raw bytes differ (re-encoding, metadata).
- `ORIENTATION_VARIANT` — Tier 3: identical only after applying EXIF orientation.
- `OFFICE_PACKAGE_EQUIVALENT` — Tier 2: same OOXML member content, repackaged (ordering,
  compression, timestamps, document properties differ).
- `ARCHIVE_REPACKAGING_VARIANT` — Tier 2: same archive member content, different packaging.

Also implemented (later phases): `PDF_TEXT_EQUIVALENT` (Tier 3), `PARTIAL_CONTENT_OVERLAP` /
`NEAR_SUBSET_CONTENT` (Tier 4, content-defined chunking), `NEAR_DUPLICATE_DOCUMENT` /
`TEXTUALLY_SIMILAR` (Tier 5, MinHash + verification), and `LIKELY_EXPORT` (Tier 6, cross-format
derivation). See `docs/advanced_features.md`.

Still deferred (design fixed, not yet emitted): `ARCHIVE_OF_DIRECTORY`, `AUDIO_*`, tabular
equivalence, and TLSH/ssdeep binary fuzzy similarity.

## What this does **not** prove

- A Tier-2/3 match is **not** byte identity and never authorizes movement on its own. Distinct
  content objects have distinct raw SHA-256 by construction, so a normalized match is always
  strictly weaker than Tier 1.
- A richer-metadata or higher-fidelity variant is never assumed disposable; the smaller/newer
  copy is not preferred automatically.

## Cost & storage

One parse per **content object** (reused across duplicate paths). Signatures are stored in
`similarity_signatures`; relationships in `content_relationships`; both are versioned and
invalidated when the algorithm or configuration fingerprint changes.

## Privacy

No raw document text is stored by the equivalence layer. Only structural/content hashes and
EXIF *presence* (never GPS coordinates) are recorded.

## Configuration

`normalization.*` (see `config/default.yaml`). Equivalence analysis is opt-in via
`housekeeper analyze normalized-content` (or the `image/office/archive-equivalence` aliases)
and supports the standard scope filters.
