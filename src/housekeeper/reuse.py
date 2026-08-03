"""Input fingerprints so identical inputs skip recomputation.

Digest = snapshot content token + config + code. Snapshot identity is the newest run that
recorded a change (not the run id — rescans allocate new ids even when disk is unchanged).
Entry-id-keyed outputs are not reusable across rescans; see ``quickstart.REUSABLE_STAGES``.
"""

from __future__ import annotations

import hashlib
from functools import lru_cache
from pathlib import Path


@lru_cache(maxsize=1)
def code_fingerprint() -> str:
    """Digest of package code and report templates that affect analysis output.

    Prefer a package digest over per-analyser version constants (easy to forget; helpers matter too).
    Whole-package scope is deliberate: any source edit re-runs stages once — safe default; narrow
    only if re-running after unrelated edits becomes costly.
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
    """Identity of analysis derived from the snapshot, for work that reads it (reports).

    Newest completed job id and count, ignoring report generation itself (a report must not
    invalidate the next report). An ``analyse``/``classify`` run therefore refreshes reports
    even without a rescan.

    Uses the jobs table (two O(1) aggregates) rather than a digest of derived rows. Trade-off:
    analysis outside a tracked job is invisible here — such callers pass ``reuse=False``.
    Every CLI, dashboard, and quickstart path records a job.
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
