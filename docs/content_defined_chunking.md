# Content-Defined Chunking (Tier 4 partial overlap)

Content-defined chunking (CDC) finds **partial byte-level overlap** between large files — regions
of shared content that exact hashing can never surface, because two files that differ by a single
byte have completely different SHA-256 digests. It is the evidence source for the Tier-4
relationships `PARTIAL_CONTENT_OVERLAP` and `NEAR_SUBSET_CONTENT`.

CDC is **opt-in** (`chunking.enabled`, default `false`) and gated to large files
(`minimum_file_size_bytes`, default 128 MiB), because it is the most expensive analyser in the
system. It never deletes or moves a file: like every analyser, the strongest outcome it can
produce is a reviewable relationship.

## Why content-defined boundaries

Fixed-size blocking (cut every N bytes) is defeated by insertion: prepend one byte to a file and
every subsequent block shifts, so no block matches its unshifted twin. Content-defined chunking
places cut points based on the *content* around each position, so an insertion only disturbs the
one chunk it lands in — the chunks before and after it are unchanged and still match. That
shift-resistance is what makes cross-file overlap detectable.

## The chunker — FastCDC gear hash

Implemented in pure Python at `src/housekeeper/chunking/python_backend.py` (`chunk_file`).

- **Rolling fingerprint.** A gear hash rolls one byte at a time:
  `fingerprint = ((fingerprint << 1) + GEAR[byte]) & 0xFFFFFFFFFFFFFFFF`. `GEAR` is a fixed table
  of 256 64-bit values seeded with the constant `0xC0FFEE`, so chunking is **deterministic and
  reproducible** across runs and machines — the same bytes always cut in the same places.
- **Normalized chunking (two masks).** From the profile's `average_chunk_size` the backend derives
  two masks: a stricter `mask_s` (harder to satisfy) used while the current chunk is *below*
  average, and a looser `mask_l` (easier) used *above* average. This biases chunk sizes toward the
  target average and tightens the size distribution versus a single-mask cut.
- **Cut rule.** A boundary is placed when the chunk reaches `maximum_chunk_size`, or when
  `size <= average and fingerprint & mask_s == 0`, or when `size > average and fingerprint &
  mask_l == 0`. `minimum_chunk_size` is always enforced first, so no chunk is smaller than the
  minimum.
- **Bounded memory.** The file is read in 1 MiB blocks and only one chunk is buffered at a time, so
  memory use is independent of file size. Each emitted chunk is hashed with SHA-256.

Each chunk becomes a `ChunkRecord(sequence_index, byte_offset, size_bytes, chunk_hash)`.

## Profiles

A `ChunkProfile` (`src/housekeeper/chunking/model.py`) captures the algorithm, its version, and the
`minimum / average / maximum` chunk sizes; its `.fingerprint()` is the SHA-256 of the sorted
parameters, so a profile change produces a distinct, non-colliding identity in storage. Profiles
come from config (`chunking.profiles.<name>`), with `chunking.default_profile` selecting one:

| Profile        | minimum | average | maximum  |
|----------------|---------|---------|----------|
| `balanced`     | 16 KiB  | 64 KiB  | 256 KiB  |
| `large_binary` | 64 KiB  | 256 KiB | 1 MiB    |

The algorithm is recorded as `fastcdc_gear` v`1`.

## Storage

Chunk data lives in derived tables (`src/housekeeper/chunking/index.py`, schema in `database.py`):

- `chunk_profiles` — one row per (name, version, fingerprint).
- `content_chunks` — the distinct chunk hashes, with an `occurrence_count`.
- `chunk_occurrences` — which content object contains which chunk, at what offset/sequence.
- `content_overlap_results` — the quantitative overlap metrics for a compared pair.

`store_chunks` is **idempotent per content object**: re-analyzing an object deletes its prior
occurrences first, so re-runs never double-count. All of this is *derived* data — see "Clearing"
below.

## From chunks to overlap relationships

`src/housekeeper/chunking/overlap.py` and `analysers/content_defined_chunks.py` turn stored chunks
into relationships without an all-pairs comparison:

1. **Candidate generation** (`generate_overlap_candidates`). An inverted index maps each chunk to
   the objects that contain it. Chunks whose `occurrence_count` exceeds
   `common_chunk_frequency_cutoff` (default 10 000) are treated as **stop chunks** and skipped —
   these are the ubiquitous runs (zero-fill, common headers) that would otherwise link everything
   to everything. Only buckets of 2–256 objects yield candidate pairs, bounding fan-out.
2. **Exact overlap** (`compute_overlap`). For each candidate pair the shared-chunk byte totals are
   computed exactly: `shared_chunk_bytes`, `overlap_a_in_b`, `overlap_b_in_a`, and a
   `weighted_jaccard`. These are recorded in `content_overlap_results`.
3. **Relationship emission** (`run_chunk_overlap_analysis`). A pair whose shared bytes clear
   `minimum_overlap_bytes` (default 64 KiB) emits a Tier-4 `content_relationships` row.
   `confidence = max(overlap_a_in_b, overlap_b_in_a)`; the type is `NEAR_SUBSET_CONTENT` when
   confidence ≥ 0.9 (one file is almost entirely inside the other) and `PARTIAL_CONTENT_OVERLAP`
   otherwise.

## Cost, estimation, and clearing

- **Estimate first.** `chunks estimate` (→ `estimate_chunk_analysis`) reports
  `candidate_content_objects`, `candidate_bytes`, `expected_chunks`, and `estimated_index_bytes`
  (≈ 96 bytes of index per chunk) before you commit to a run. At the default 64 KiB average, expect
  roughly one chunk hash per 64 KiB of eligible data.
- **Clear derived data.** `derived-data clear CHUNK_INDEX` (→ `clear_chunk_index`) removes only the
  chunk tables and `content_overlap_results`. It never touches source inventory, classifications,
  or review decisions — chunking can always be re-derived.

## Guarantees and limits

- **Never implies byte identity.** Distinct content objects always have distinct SHA-256; a Tier-4
  overlap is explicitly weaker than a Tier-1 exact duplicate and is labeled as such.
- **Deterministic.** The fixed gear seed means identical inputs always chunk identically.
- **Bounded.** Stop-chunk suppression and bucket-size caps keep candidate generation sub-quadratic;
  memory is capped by the 1 MiB read window.
- **Review-only.** Overlap relationships feed review and the graph's partial-overlap projection
  (see `docs/graph_model.md`); they never trigger movement on their own.

## Related

- `docs/advanced_features.md` — one-paragraph operational summary.
- `docs/document_similarity.md` — the Tier-5 text near-duplicate path (a different technique for a
  different question).
- `docs/duplicate_taxonomy.md` — where Tier 4 sits among the evidence tiers.
- `docs/performance.md`, `benchmarks/` — measuring chunking cost.
