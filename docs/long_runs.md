# Long, sustained runs

This is the operational guide for pointing `housekeeper` at storage measured in terabytes, where a
single scan can run for hours or days. The tool is built to survive that — every stage is durable,
pausable, and resumable — but a few settings and expectations are worth knowing before you start.

## Pick a storage profile (or let it measure)

Hashing and traversal concurrency come from the storage profile
(`performance.storage_profile`). The default is `auto`, which starts conservative (one hash worker,
one traversal worker) and then *promotes* a source after it has measured that source's real
throughput — `hdd → ssd → nvme` over successive runs, never more concurrency than a measurement has
justified. If you already know the drive, set it explicitly to skip the ramp:

```yaml
performance:
  storage_profile: ssd     # or nvme, hdd, network
```

Or depart from a profile on one axis without redefining it:

```yaml
performance:
  storage_profile: nvme
  overrides:
    full_hash_workers: 12   # you measured your device saturates here
```

| profile | full_hash_workers | traversal_workers | parser_workers |
|---|---|---|---|
| hdd | 1 | 1 | 2 |
| ssd | 8 | 4 | 4 |
| nvme | 16 | 8 | 6 |
| network | 1 | 4 | 2 |

The worker counts are defaults pending a per-machine `benchmarks/benchmark_hashing.py --workers`
sweep; on a rotational disk, one hash worker and one traversal worker are correct because concurrent
reads there are competing seeks, not throughput.

## Hard-linked backup drives

If your drive is a snapshot-style backup — rsnapshot, Time Machine, `cp -al` rotations — where each
retained snapshot is a hard link to one inode, `hashing.hardlink_identity_reuse` (on by default)
reads each inode's data **once** and copies the verified result to every other link. On a drive of
ten hard-linked snapshots this is the difference between reading the data once and ten times. It is
gated on `nlink > 1`, so it can only ever affect files the filesystem itself reports as shared.

## Pause, cancel, resume

Every stage runs inside a durable job. Pause or cancel from the dashboard or `housekeeper jobs`, or
press Ctrl-C; the run parks at the next checkpoint with everything committed, and a re-run continues:

* **Scan resume is proportional to what's left.** An interrupted scan records its traversal frontier
  (the pending directories) at every batch boundary, so a resume continues from where it stopped
  rather than re-walking from the root. On a billion-file tree that is the difference between
  re-paying day one and not.
* **Stage resume continues rather than redoes.** A quickstart's finished stages are skipped on
  resume; only the interrupted one re-runs.
* **The set-based scan epilogue is windowed.** Parent-linking, change classification, signature
  copy-forward and missing-detection run in 250k-row committed windows, so a cancel lands within a
  window (seconds), the WAL is settled per window instead of growing to the size of the whole diff,
  and a failure near the end loses one window rather than hours. Re-running a window is idempotent.

## A drive that drops off the bus

`scanner.pause_after_consecutive_errors` (default 1000) parks the scan — resumably — after a run of
consecutive unreadable entries, the signature of a drive that disconnected mid-scan. Without it, a
dead mount records millions of per-entry errors and keeps walking for hours. A single readable entry
resets the count, so scattered permission errors never trip it. Set it to 0 to disable.

Bind-mount and symlink loops within one filesystem are bounded by a directory cycle guard (a
device+inode visited-set, capped by `scanner.cycle_guard_max_directories`); `stay_on_filesystem`
covers cross-device loops.

## Disk headroom for the workspace

Keep the workspace (the SQLite database and its WAL) on a disk with room to grow. Guidance:

* The database grows by roughly one snapshot's worth of rows per scan — history is retained by
  design. Bound it with `housekeeper database prune-snapshots --keep N`, which removes superseded
  snapshots **and everything derived from them**, including the per-entry change log; it refuses to
  touch any snapshot a review decision references.
* The WAL is kept bounded during long stages: analyser stages checkpoint at every boundary, and the
  long identity/artifact read streams are keyset-paged so a reader never pins the log across the
  whole stage. Even so, allow a few GB of WAL headroom for a very large single stage.
* New databases are created with an 8 KiB page size (shallower B-trees at scale). An older workspace
  adopts it on `housekeeper database vacuum`.

## What to measure before trusting a number here

Nothing in this document is a portability promise. Before believing any performance claim on your
own hardware:

```bash
# Build a plan-quality metadata corpus (no real files) and profile its on-disk shape.
PYTHONPATH=src python benchmarks/generate_metadata_database.py /tmp/corpus.sqlite --entries 100000000
PYTHONPATH=src python benchmarks/generate_metadata_database.py /tmp/corpus.sqlite --profile-only

# Soak the real set-based rescan diff at that scale and read its cost curve.
PYTHONPATH=src python benchmarks/soak_rescan.py /tmp/corpus.sqlite --rescans 10
```

The soak driver prints statements, commits, seconds and WAL peak per rescan — the numbers that tell
you whether the diff and the WAL stay bounded at *your* scale, which is the only scale that matters.
