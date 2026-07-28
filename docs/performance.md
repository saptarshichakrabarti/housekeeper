# Performance

The pipeline uses streamed hashing, size candidate funnels, SQLite WAL, content-link reuse, bounded worker queues, isolated parser timeouts, keyset-style dashboard pagination, set-based scan diffing, and graph limits. Incremental scans reuse verified links when the prior stat fingerprint is unchanged and record change evidence. Materialized summaries keep repeated dashboard overview reads away from raw inventory tables.

## Recorded synthetic baseline

On 2026-07-16, the current development environment scanned the generated 10,000-entry fixture in **0.7516 seconds** (100,000 logical bytes; no directory hierarchy). This is a local synthetic metadata/I/O smoke measurement, not a portability promise. Reproduce it with:

```bash
PYTHONPATH=src python benchmarks/generate_large_fixture.py /tmp/housekeeper-benchmark-fixture --files 10000
PYTHONPATH=src python benchmarks/benchmark_scan.py /tmp/housekeeper-benchmark-fixture
```

`generate_metadata_database.py --entries 1000000` creates the large simulated metadata corpus. Run database, dashboard, and graph probes against that database; compare results only with a baseline collected on the same host/storage profile. CI tests query shape and hard limits rather than fragile absolute timing.

## Measured on a real inventory

Numbers below are from a 2.04 GB / 1,304,682-entry inventory of `~/Pictures` and `~/Downloads`
(1.24M files, 47 GB of source data) on an Apple-silicon laptop, rehearsed on a byte copy. They are
recorded because two stages could not complete at all before this work, and a synthetic corpus said
nothing was wrong.

| Stage | Before | After |
|---|---|---|
| `run_project_analysis` | 58.3 h | **54.0 s** |
| `run_directory_overlap_analysis` | 60.1 h | **210.3 s** |
| v6 → v9 migration | did not complete in 11 min | **75 s** |
| SQL statements per entry, unchanged rescan | ~19 | **1.044** |
| commits, 1,316,010-entry rescan | one per entry | **276** |
| bytes re-read, unchanged rescan | 4× the corpus | **87 KB of 23.6 GB** |
| stat calls | 4 per entry | **1 per entry** |
| index storage | — | **−365 MB** |

The `metadata_corpus` test fixture (120k rows, two snapshots, real statistics, built directly rather
than as files) is the cheapest way to reproduce plan-quality conditions. It is not a substitute for
one run against production data before believing a performance change.

### What guards this

Each of these exists because the corresponding failure actually happened. The most likely way to
lose the work is to weaken one when it becomes inconvenient.

| Guard | Catches |
|---|---|
| `tests/test_query_plans.py` | a predicate or index change that turns a seek back into a scan, including in migrations. Asserted against *generated* SQL, not a hand-typed copy — the copy stayed correct while the real query regressed. |
| `tests/test_work_counters.py` | per-entry SQL, per-object commits (measured per stage), re-reading unchanged bytes, a second read for identity |
| `tests/test_snapshot_isolation.py` | a current-state consumer reading base tables instead of the views; an analyser that skips work after a rescan |
| `tests/test_corpus_shapes.py` | cost or output that tracks how often the tool has run rather than the size of the drive (20 rescans) |
| `tests/test_dashboard_indexes.py` | an index that stops being chosen, and one that has stopped earning its size |
| `tests/test_images.py` | a perceptual descriptor that stops surviving re-encoding, or stops separating different pictures |
| `tests/test_configuration_honesty.py` | a knob added without being wired |
| `tests/test_acceleration.py` | a native backend that disagrees with Python, or a client that forks per request |
| `benchmarks/baseline.json` | a change that alters entity counts |

## Decisions taken and not revisited

These were measured and closed. Each records the number that decided it, so reopening one means
re-deriving that number rather than re-arguing the design.

**Native acceleration is not wired into hashing.** SHA-256 is **5%** of the identity stage on real
files (median 692 bytes); open+read is 95%. A hasher that took zero time would save 4%, while the IPC
round trip to a persistent backend costs 0.57 ms per file against 0.243 ms to hash it in-process — so
wiring it in measured **2.3× slower**. The Rust core and its client are maintained and tested for
byte-exact agreement with Python anyway, because an accelerator that is wrong is worse than one that
is slow. Reopen only for a corpus of *large* files, and re-derive the 5% first. See
`docs/rust_boundary.md`.

**There is no materialised `directory_content` relation.** It would cost ~364 MB (36.4 bytes/row
measured, ~10M rows) to replace two stages costing 84.9 s per run combined. The per-directory queries
it was originally proposed to remove are gone by a different route: project marker detection is one
query for the stage instead of two per directory (**17×** measured), and the one genuinely recursive
step runs only for directories that turned out to be projects. Revisit if a third stage starts
walking the tree.

**Perceptual candidate generation is bucketed streaming, not a SQL join.** Measured on 20,000
descriptors: a SQL self-join over bands returned 13.7M rows in 68 s; the same join with distance
filtered by a registered SQL function took 17–23 s; streaming the 180,000 band rows in bucket order
and comparing in place took **4.3 s**. All three return the identical 78,703 pairs. The join is not
the expensive part (0.9 s) — handing back one row per shared band per pair is.

**Collapsing identical descriptors before comparison was tried and reverted.** It does cut
comparisons (1.9M → 0.1M on 5,000 objects sharing 100 descriptors), but comparisons are not the cost;
emitting k(k−1)/2 *pairs* is, and that is inherent to pairwise output. Measured 1.2× at best and
**0.7× at worst**, for more code.

**Scope is defaulted, not required.** A required `scope` parameter can still be handed
`AnalyserScope()` meaning "all history", so `scope=None` resolves to the current inventory instead and
history is an explicit `AnalyserScope.all_history()`. The safe path is the default path rather than
merely the mandatory one.

**Historical classifications and lifecycle rows are retained.** A superseded snapshot's verdict is
the audit trail, so the scoped rebuild does not delete it. What is enforced is that derived
*collections* never span snapshots and that a rescan does not re-derive verdicts for older rows;
bounding the history is `housekeeper database prune-snapshots`, which is explicit and refuses to
touch anything a review decision references.

**`content_objects`, `relationships`, `content_relationships` and the graph are global.** Content
identity is snapshot-independent by design — that is what makes a file recognisable across drives and
rescans — so these are not scoping bugs.
