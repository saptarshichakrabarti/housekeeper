"""Input fingerprints: work whose inputs are provably identical is not redone.

Three things decide whether a completed stage's output still stands: the *content* of the snapshot
it read, the configuration, and the code. All three go into one digest, and a stage or report whose
digest matches a completed one is reused instead of recomputed.

The subtle input is the snapshot. A rescan writes a whole new set of ``filesystem_entries`` rows, so
the run id is useless as an identity — it changes even when nothing on disk did. The token here
names the *content* instead: the newest run of the source that actually recorded a change. Two scans
of an unchanged tree therefore agree, and a chain of unchanged rescans keeps agreeing.

What this does NOT make reusable is anything keyed to entry ids. A rescan's entries are new rows, so
classifications, duplicate members, projects and canonical roles must be re-derived for the new
snapshot or the ``current_*`` views come back empty — see ``quickstart.REUSABLE_STAGES``.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def code_fingerprint() -> str:
    """Digest of the code that produces analysis output and reports.

    A digest of the package beats a hand-maintained version constant per analyser: a constant has to
    be remembered on every semantic change, and a stage's output also depends on the shared helpers
    it calls. Report templates count as code — a report changes when its template does.

    ponytail: whole-package digest, so any source edit re-runs every stage once. That is the safe
    direction; narrow it to per-analyser dependency sets only if re-running after an edit ever hurts.
    """
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for path in sorted(p for pattern in ("*.py", "*.j2") for p in root.rglob(pattern)):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def snapshot_token(database, scan_run_id: int) -> str:
    """Identity of the *content* of a snapshot, rather than of the run that recorded it.

    The newest completed run of this source that recorded a change — the run whose content this
    snapshot still equals — falling back to the source's first run when no change was ever recorded
    (a source scanned once and rescanned unchanged).
    """
    row = database.fetch_one(
        """SELECT COALESCE(
             (SELECT MAX(ch.scan_run_id) FROM scan_entry_changes ch
                JOIN scan_runs r ON r.id=ch.scan_run_id
              WHERE ch.change_status<>'UNCHANGED' AND r.status='COMPLETE'
                AND r.source_root_fingerprint=cur.source_root_fingerprint),
             (SELECT MIN(r2.id) FROM scan_runs r2 WHERE r2.status='COMPLETE'
                AND r2.source_root_fingerprint=cur.source_root_fingerprint)
           ) token FROM scan_runs cur WHERE cur.id=?""",
        (scan_run_id,),
    )
    token = row["token"] if row else None
    return str(token if token is not None else scan_run_id)


def inventory_token(database) -> str:
    """Content identity of the whole current inventory — every source's current snapshot."""
    runs = database.fetch_all(
        "SELECT latest_complete_scan_run_id id FROM source_roots "
        "WHERE latest_complete_scan_run_id IS NOT NULL ORDER BY id"
    )
    return ",".join(snapshot_token(database, int(row["id"])) for row in runs)


def derived_state_token(database) -> str:
    """Identity of the analysis derived from the snapshot, for work that reads it (reports).

    The tool's own record of finished work: the newest and the number of completed jobs, ignoring
    report generation itself (a report job must not invalidate the next report). An ``analyse`` or
    ``classify`` run therefore refreshes reports even without a rescan.

    ponytail: the jobs table rather than a digest of the derived rows — two O(1) aggregates instead
    of a scan per analysis table. The trade-off is real and bounded: analysis performed *outside* a
    tracked job (a library caller invoking an analyser directly) is invisible here, so such a caller
    passes ``reuse=False``. Every CLI, dashboard and quickstart path records a job.
    """
    row = database.fetch_one(
        "SELECT COALESCE(MAX(id),0) newest, COUNT(*) n FROM jobs "
        "WHERE status LIKE 'COMPLETED%' AND job_type<>'REPORT_GENERATION'"
    )
    return f"{row['newest']}:{row['n']}" if row else ""


def input_fingerprint(label: str, token: str, config_fingerprint: str) -> str:
    """The digest a completed unit of work is looked up by."""
    # NUL-separated, so no combination of values can spell out another combination.
    joined = f"{label}\0{token}\0{config_fingerprint}\0{code_fingerprint()}"
    return hashlib.sha256(joined.encode()).hexdigest()
