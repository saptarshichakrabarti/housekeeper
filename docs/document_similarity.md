# Document Similarity — MinHash / LSH (Tier 5)

Document similarity finds **near-duplicate prose** — two documents that say almost the same thing
even though their bytes differ (a reformatted export, a lightly edited revision, the same text in
`.txt` and `.docx`). It produces the Tier-5 relationships `NEAR_DUPLICATE_DOCUMENT` and
`TEXTUALLY_SIMILAR`.

This is a **probabilistic** technique, and the pipeline treats it that way: MinHash + LSH only
*generate candidates*; every candidate is then confirmed with an **exact** set-similarity
computation before any relationship is written. A probabilistic signal alone never becomes a
recorded relationship, and it never becomes an exact-duplicate classification.

The whole path is dependency-free (standard-library hashing only) and deterministic.

## The pipeline

Orchestrated by `src/housekeeper/analyzers/document_minhash.py`
(`run_document_minhash_analysis`), over documents with suffixes
`.txt .md .csv .rst .log .docx .pdf`:

1. **Ensure text.** Document content objects are hashed if needed, then normalized text is
   extracted via `analyzers/documents.extract_document` (the same normalization used elsewhere).
2. **Tokenize** (`similarity/shingling.py`). Text is NFKC-normalized, casefolded, and split into
   `\w+` tokens. Documents with fewer than `minimum_tokens` (default 20) tokens are **skipped** —
   too little text to judge similarity honestly.
3. **Shingle.** Tokens become a set of overlapping word n-grams of length `shingle_size`
   (default 5): `word_shingles`. Two documents' shingle sets overlap in proportion to how much
   contiguous phrasing they share. (If a document has fewer tokens than the shingle size, its token
   set is used directly.)
4. **MinHash** (`similarity/minhash.py`). Each shingle set is reduced to a signature of
   `minhash_permutations` (default 128) 64-bit minima. Uses universal hashing
   `(a·x + b) mod (2⁶¹−1)` with deterministic `(a, b)` pairs (seed `0xA5A5`) and blake2b shingle
   hashes, so signatures are reproducible run-to-run. The fraction of equal signature positions is
   an unbiased estimator of the true Jaccard similarity of the underlying sets.
5. **LSH candidate generation** (`similarity/lsh.py`). Signatures are banded so that only documents
   likely to exceed the target similarity land in the same bucket. `choose_bands` picks the
   band/row split that puts the S-curve midpoint closest to `lsh_threshold` (default 0.75); only
   buckets of 2–512 members emit candidate pairs, bounding fan-out. Documents that share no bucket
   are never compared.
6. **Exact verification.** For each candidate pair the **exact** shingle-Jaccard is computed
   (`exact_jaccard`). Only pairs at or above `verification_threshold` (default 0.8) produce a
   relationship. This is the step that rejects shared boilerplate, template headers, and
   coincidental LSH collisions — MinHash proposes, exact Jaccard disposes.
7. **Emit.** A verified pair writes a Tier-5 `content_relationships` row with
   `confidence = exact Jaccard`. The type is `NEAR_DUPLICATE_DOCUMENT` when the exact score ≥ 0.95
   and `TEXTUALLY_SIMILAR` otherwise.

## Configuration (`document_similarity`)

| Key                     | Default | Meaning                                                        |
|-------------------------|---------|----------------------------------------------------------------|
| `enabled`               | `true`  | Master switch for the analyzer.                                |
| `shingle_type`          | `word`  | Shingling unit.                                                |
| `shingle_size`          | `5`     | Words per shingle.                                             |
| `minhash_permutations`  | `128`   | Signature length; higher = tighter Jaccard estimate, more cost.|
| `lsh_threshold`         | `0.75`  | Similarity the LSH banding is tuned to surface as a candidate. |
| `verification_threshold`| `0.8`   | Exact-Jaccard floor a relationship must clear.                 |
| `minimum_tokens`        | `20`    | Documents shorter than this are skipped.                       |

Note `lsh_threshold` (candidate recall) is intentionally *below* `verification_threshold` (the
decision gate): LSH is allowed to over-propose, and exact verification is the authority.

## Storage

- `similarity_signatures` — one `TEXT_MINHASH` signature per document content object, keyed by
  (content object, signature type, version, configuration fingerprint), so a config change
  produces distinct signatures rather than colliding with stale ones. Signature version is `1`.
- Relationships land in `content_relationships` at Tier 5.
- `clear_minhash_index` removes only `TEXT_MINHASH` signatures — derived data, always
  re-computable, never touching source inventory or review decisions.

## Guarantees and limits

- **Candidates only, then verify.** No relationship exists without exact-Jaccard confirmation ≥
  `verification_threshold`. The probabilistic layer cannot, by itself, classify anything.
- **Never an exact duplicate.** Tier-5 similarity is strictly weaker than Tier-1 byte identity and
  is labeled as such; it never collapses two content objects into one.
- **Boilerplate-resistant.** Shared templates and headers that fool the LSH bucketing are removed
  by exact verification.
- **Deterministic and dependency-free.** Fixed permutation and hash seeds; standard library only.
- **Bounded.** LSH bucketing plus bucket-size caps keep comparison sub-quadratic; short documents
  are excluded rather than guessed at.
- **Review-only.** Results feed review and the graph's document-family projection
  (see `docs/graph_model.md`); they never trigger movement.

## Related

- `docs/advanced_features.md` — one-paragraph operational summary.
- `docs/content_defined_chunking.md` — Tier-4 byte-region overlap (a different question: shared
  bytes in large files, not shared meaning in prose).
- `docs/format_equivalence.md` — Tier 2–3 normalized equivalence (e.g. PDF page-text), which proves
  *more* than textual similarity for the formats it covers.
- `docs/duplicate_taxonomy.md` — where Tier 5 sits among the evidence tiers.
