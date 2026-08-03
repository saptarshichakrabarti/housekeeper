# Performance

The pipeline uses streamed hashing, size candidate funnels, SQLite WAL, content-link reuse, bounded worker queues, isolated parser timeouts, keyset-style dashboard pagination, set-based scan diffing, and graph limits. Incremental scans reuse verified links when the prior stat fingerprint is unchanged and record change evidence. Materialized summaries keep repeated dashboard overview reads away from raw inventory tables.

## Scaling to terabytes and multi-day runs

A second body of work targets storage measured in terabytes and scans that run for days. It is
operational-facing — see [long_runs.md](long_runs.md) — and rests on these mechanisms:

* **One shared, parallel identity service** (`core/identity.py`). Full+quick digest, content link
  and signature row for every candidate, hashed across `full_hash_workers` threads. The five serial
  `compute_full_hash` loops (dedup candidates, marginal value, document minhash, cross-format
  derivation, content-defined chunks, normalized content) and the content-analysis identity loop all
  route through it; the dedup candidate pass — which runs before content analysis and did the bulk of
  a first scan's byte volume on the main thread — is now parallel. It streams, so it never
  materialises the candidate list.
* **Hard-link identity reuse.** One representative per `(device, inode)` is hashed and the verified
  result written for every hard link to it (gated on `nlink > 1` and `hashing.hardlink_identity_reuse`).
  A backup drive of N hard-linked snapshots reads the data once, not N times.
* **A worker-count ladder.** `nvme`/`ssd` profiles with more hash and traversal workers; `auto`
  promotes a source `hdd → ssd → nvme` from measured throughput, never beyond what it observed.
* **Page-cache hygiene.** `O_NOATIME` and `posix_fadvise(SEQUENTIAL/RANDOM)` on every hash read;
  `POSIX_FADV_DONTNEED` after hashing content a parser will never re-open, so a terabyte streamed
  through the cache does not evict the database's own hot pages. All degrade cleanly where absent.
* **Traversal hardening.** A directory cycle guard (bind-mount loops terminate), a flat-directory
  bound (a folder over `SCAN_DIR_SORT_LIMIT` is streamed unsorted, not held to sort), and an
  error-storm breaker (`scanner.pause_after_consecutive_errors`) that parks a scan of a dead drive.
* **Frontier resume.** `scan_runs.frontier_json` records the pending-directory stack at batch
  cadence, so a resume is O(remaining), not O(tree).
* **Device-aware parallel traversal.** `traversal_workers` offloads scandir+stat (which release the
  GIL) to a pool while every mutation stays on one thread. First-run Linux/macOS device metadata
  selects SSD/NVMe profiles; unknown and rotational devices retain one serial walker.
* **Windowed scan epilogue.** `execute_windowed` runs parent-linking, change classification,
  signature copy-forward and missing detection in 250k-row committed windows behind a
  `UNIQUE(scan_run_id, entry_id)` index, so the diff is interruptible, WAL-bounded, and idempotent on
  resume — instead of one multi-hour transaction.
* **Keyset-paged identity streams.** `iter_keyset` pages the long hashing streams so a reader never
  pins the WAL across a whole stage; the composite `(device, inode, id)` key preserves hard-link
  adjacency.
* **A partial files-only size index** and an **8 KiB page size** for new databases.
* **BLAKE3 as the default hash** (`new_hasher`; the wheel is a required dependency and the Rust core
  implements it too) plus a `stage_ms:hash_cpu`/`stage_ms:hash_io` split, so the ongoing "is a faster
  hash worth it" question stays re-derivable by measurement rather than argued.

### Decisions deferred by measurement, not built

Consistent with the section below, two items the plan raised are recorded here as deliberately not
built, with the reason:

* **No mid-statement progress handler.** Windowing already bounds each epilogue statement to seconds,
  so Ctrl-C lands between windows. A `set_progress_handler` interrupt would additionally abort a long
  `refresh_materialized_summaries` or `ANALYZE`, but a stuck interrupt Event would abort the very
  cleanup statements a failure path runs — so the footgun outweighs interrupting a bounded aggregate.
* **`_work_plan`'s artifact stream keeps its single cursor.** Its `GROUP BY co.id` shape needs a
  different keyset than the identity streams, and the identity hashing streams are the longer WAL
  pin. Revisit if the parse stage's WAL growth is measured to matter.

### Reserved for a measured gate

Path interning (a v11 `paths` table to shrink per-snapshot bytes ~5–10×), a Rust parallel *walker*,
and io_uring are all left for a decision driven by the
100M-row `generate_metadata_database.py --profile-only` shape and the `soak_rescan.py` cost curve —
the instruments are shipped; the structural changes are not, because at the measured small-file
scale they are not yet justified.

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
| `tests/test_stage_reuse.py` | stage reuse that skips work the current snapshot still needs, or fails to skip after an unchanged rescan |
| `tests/test_identity_service.py` | the shared hasher diverging from the real digest, a second read for the quick hash, or a result that depends on the worker count |
| `tests/test_hardlink_identity.py` | a hard-linked inode read more than once, or reuse hiding that the copies exist |
| `tests/test_scanner_robustness.py` | a bind-mount loop that never terminates, a flat directory materialised whole, or an error storm walked to the end |
| `tests/test_frontier_resume.py` | a resume that re-walks the whole tree, or a resumed inventory that differs from a clean scan's |
| `tests/test_parallel_traversal.py` | the parallel walk diverging from the serial inventory or stat count, or deadlocking on cancel |
| `tests/test_windowed_diff.py` | window size changing the diff, or a re-executed window duplicating change rows |
| `tests/test_keyset.py` | a keyset page skipping or duplicating a row, or breaking hard-link adjacency |

## Stage reuse on a re-run

A quickstart re-run reuses a completed stage whose *input fingerprint* matches:
`sha256(stage label, snapshot token, configuration fingerprint, code digest)` — recorded in the
stage's job scope and looked up before the stage runs (`src/housekeeper/reuse.py`).

* The **snapshot token** names the content of a snapshot, not the run that recorded it: the newest run
  of that source which recorded a change. Two scans of an unchanged tree therefore agree, and a chain
  of unchanged rescans keeps agreeing — a run id would change every time and reuse would never fire.
* The **code digest** is a hash of the package's `*.py` and `*.j2` files rather than a version
  constant per analyser. A constant has to be remembered on every semantic change; the digest cannot
  be forgotten and also covers shared helpers. The cost is that any source edit re-runs every stage
  once, which is the safe direction.

**Only content-object-keyed stages are reusable** (`quickstart.REUSABLE_STAGES`: content-analysis,
document-versions, image-similarity). A rescan writes a whole new set of `filesystem_entries` rows, so
anything keyed to an entry id — classifications, duplicate members, projects, canonical roles,
directory overlap — must be re-derived for the new snapshot or the `current_*` views come back empty.
That is a correctness boundary, not a tuning choice: extending the set means proving the stage's
output is not keyed to entry ids.

The reuse check rests on each *stage job* reaching `COMPLETED`, not on the pipeline's own state. That
is what makes **Resume** continue rather than redo: an interrupted run's finished stages are exactly
the ones worth skipping, and a cancelled stage has no fingerprint to match. The one thing an
interrupted predecessor does switch off is the changed-only narrowing of content analysis — an
artifact the previous run still owed is invisible in the change record, so narrowing to changed
entries would skip it forever. The summary reports both decisions (`mode`, `changed_only`).

Reports use the same fingerprint plus the analysis that has been recorded since, so `report all` on an
untouched workspace writes nothing at all — the nine HTML reports carry their marker in the file
itself, and the CSV/JSONL exports carry it in a `.fingerprint` sidecar (CSV has no comment syntax, and
a JSONL stream must stay parseable). Delete any output and only that one regenerates. `--full` forces
every stage and every report.

Parallel structural stages are still **not built**: SQLite WAL allows one writer, and the decision
gate is a measurement — a full quickstart still spending >50% of wall-clock in structural stages
*after* incremental re-runs land.

## Decisions taken and not revisited

These were measured and closed. Each records the number that decided it, so reopening one means
re-deriving that number rather than re-arguing the design.

**Native hashing is selected when available.** Platform wheels bundle the Rust core, including a
one-read identity operation that produces full and sampled digests together. A missing,
incompatible, or failed subprocess falls back to Python. The reference Python implementation
remains in use for cache-dropping and instrumented identity runs because those semantics are not
yet represented by the Rust protocol.
Wheel CI builds and exercises the core for each supported CPython/platform target. If no matching
wheel exists, the source distribution remains installable without Cargo and uses Python; installing
from source with Cargo available includes the core.
The earlier small-file measurement still applies: SHA-256 was **5%** of identity time (median 692
bytes), so re-measure the end-to-end effect on a large-file corpus before treating this as a
performance win. The default is now BLAKE3, which caps that 5% lower still, but on a small-file
corpus the win is bounded by the same 5% — the reason to default to it is that large-file corpora
are where identity actually hurts, not that it changes the measured small-file number. Existing
workspaces keep the algorithm they were inventoried with (`workspace_hash_algorithm`). See
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
