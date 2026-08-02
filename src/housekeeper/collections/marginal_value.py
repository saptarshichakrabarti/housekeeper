"""Backup / collection marginal preservation value and non-destructive removal simulation."""

from __future__ import annotations

import json
from collections import defaultdict

from ..constants import ClusterType, CollectionValue


def _ensure_all_hashed(database, config, scope) -> None:
    """Hash every unlinked file so uniqueness is measured over content, not just dup candidates."""
    from ..core.identity import ensure_content_identity

    entry_sql, params = scope.entry_id_sql()
    stream = database.reader().iter_rows(
        f"""SELECT e.id,e.scan_run_id,e.absolute_path,e.device_id,e.inode_or_file_id,e.nlink
           FROM filesystem_entries e LEFT JOIN entry_content_links l ON l.entry_id=e.id
           WHERE e.entry_type='file' AND l.entry_id IS NULL AND e.id IN ({entry_sql})
           ORDER BY e.device_id,e.inode_or_file_id,e.id""",
        params,
    )
    ensure_content_identity(database, config, stream, progress_phase="hashing for uniqueness")


def _collection_membership(database, scope):
    """Return (appears_in, collection_objects, sizes, protected, error) maps keyed by content id."""
    appears_in: dict[int, set[tuple[str, str]]] = defaultdict(set)
    collection_objects: dict[tuple[str, str], set[int]] = defaultdict(set)
    entry_sql, params = scope.entry_id_sql()
    for row in database.iter_rows(
        f"""SELECT DISTINCT l.content_object_id AS cid, e.source_root AS root,
              CASE WHEN instr(e.relative_path,'/')=0 THEN e.relative_path
                   ELSE substr(e.relative_path,1,instr(e.relative_path,'/')-1) END AS top_level
           FROM entry_content_links l JOIN filesystem_entries e ON e.id=l.entry_id
           WHERE e.entry_type='file' AND e.id IN ({entry_sql})""",
        params,
    ):
        key = (str(row["root"]), str(row["top_level"]))
        appears_in[int(row["cid"])].add(key)
        collection_objects[key].add(int(row["cid"]))
    sizes = {int(r["id"]): int(r["size_bytes"]) for r in database.iter_rows("SELECT id,size_bytes FROM content_objects")}
    protected: set[int] = {
        int(r["cid"])
        for r in database.iter_rows(
            """SELECT DISTINCT l.content_object_id AS cid FROM entry_content_links l
               JOIN classifications c ON c.entry_id=l.entry_id WHERE c.classification IN ('PROTECTED','ERROR')"""
        )
    }
    return appears_in, collection_objects, sizes, protected


def _classify_value(total_bytes: int, unique_bytes: int, unique_count: int) -> str:
    if total_bytes == 0:
        return CollectionValue.UNRESOLVED
    if unique_bytes == 0:
        return CollectionValue.FULLY_CONTENT_REDUNDANT_CONTEXT_REMAINS
    ratio = unique_bytes / total_bytes
    if ratio < 0.05:
        return CollectionValue.MOSTLY_REDUNDANT_WITH_UNIQUE_ITEMS
    if ratio < 0.25 and unique_count < 10:
        return CollectionValue.LOW_BYTE_VALUE_HIGH_CONTEXT_VALUE
    if ratio < 0.5:
        return CollectionValue.MODERATE_MARGINAL_VALUE
    return CollectionValue.HIGH_MARGINAL_VALUE


def run_backup_value_analysis(database, config, scope=None, job_id=None) -> dict[str, int]:
    from ..analysers.scope import resolve_scope
    from ..jobs import checkpoint

    scope = resolve_scope(database, scope)
    _ensure_all_hashed(database, config, scope)
    appears_in, collection_objects, sizes, protected = _collection_membership(database, scope)
    created = 0
    for index, (key, cids) in enumerate(collection_objects.items(), 1):
        checkpoint(database, job_id, processed_count=index)
        unique = [cid for cid in cids if appears_in[cid] == {key}]
        total_bytes = sum(sizes.get(cid, 0) for cid in cids)
        unique_bytes = sum(sizes.get(cid, 0) for cid in unique)
        unique_protected = sum(1 for cid in unique if cid in protected)
        summary = {
            "total_content_objects": len(cids),
            "unique_content_objects": len(unique),
            "total_bytes": total_bytes,
            "unique_bytes": unique_bytes,
            "unique_protected_objects": unique_protected,
            "value_class": _classify_value(total_bytes, unique_bytes, len(unique)),
        }
        name = f"{key[1]} @ {key[0]}"
        database.connect().execute(
            """INSERT INTO collection_clusters(cluster_type,name,confidence,algorithm,algorithm_version,scope_json,summary_json)
               VALUES(?,?,?,?,?,?,?)
               ON CONFLICT(cluster_type,name) DO UPDATE SET summary_json=excluded.summary_json,scope_json=excluded.scope_json""",
            (
                ClusterType.BACKUP_FAMILY,
                name,
                1.0,
                "marginal_value",
                "1",
                json.dumps({"source_root": key[0], "top_level": key[1]}, sort_keys=True),
                json.dumps(summary, sort_keys=True),
            ),
        )
        created += 1
    database.connect().commit()
    return {"collections": created}


def simulate_removal(database, collection_id: int, scope=None) -> dict:
    """Non-destructive: report what a collection uniquely contributes and would lose if removed."""
    from ..analysers.scope import resolve_scope

    cluster = database.fetch_one(
        "SELECT scope_json,name FROM collection_clusters WHERE id=?", (collection_id,)
    )
    if not cluster:
        raise ValueError(f"unknown collection {collection_id}")
    # The cluster's own stored scope (which drive, which top-level directory) — not the analyser
    # scope, which decides *which snapshot* the membership counts come from.
    cluster_scope = json.loads(cluster["scope_json"])
    key = (cluster_scope.get("source_root", ""), cluster_scope.get("top_level", ""))
    appears_in, collection_objects, sizes, protected = _collection_membership(
        database, resolve_scope(database, scope)
    )
    cids = collection_objects.get(key, set())
    unique = [cid for cid in cids if appears_in[cid] == {key}]
    redundant = [cid for cid in cids if len(appears_in[cid]) > 1]
    return {
        "collection": cluster["name"],
        "content_losing_all_copies": len(unique),
        "unique_bytes_lost": sum(sizes.get(cid, 0) for cid in unique),
        "unique_protected_or_error_objects": sum(1 for cid in unique if cid in protected),
        "apparent_recoverable_bytes": sum(sizes.get(cid, 0) for cid in redundant),
        "note": "SIMULATION ONLY — no files are moved or deleted; review unique/protected items before any action.",
    }
