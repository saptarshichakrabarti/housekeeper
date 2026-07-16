from collections import defaultdict
from pathlib import Path

from ..config import AppConfig
from ..database import Database
from ..hashing import compute_full_hash


def run_exact_duplicate_analysis(database: Database, config: AppConfig) -> None:
    rows = database.fetch_all(
        "SELECT id,absolute_path,size_bytes FROM filesystem_entries WHERE entry_type='file' ORDER BY id"
    )
    by_size = defaultdict(list)
    for r in rows:
        by_size[r["size_bytes"]].append(r)
    database.connect().execute("DELETE FROM exact_duplicate_members")
    database.connect().execute("DELETE FROM exact_duplicate_groups")
    for size, candidates in by_size.items():
        if len(candidates) < 2:
            continue
        by_hash = defaultdict(list)
        for r in candidates:
            h = compute_full_hash(
                Path(r["absolute_path"]),
                config.section("hashing")["algorithm"],
                config.section("hashing")["full_hash_block_bytes"],
            )
            database.connect().execute(
                "INSERT OR REPLACE INTO file_signatures(entry_id,full_hash,hash_algorithm,hash_status,hash_error,full_hash_computed_at) VALUES(?,?,? ,?,?,CURRENT_TIMESTAMP)",
                (
                    r["id"],
                    h.digest,
                    config.section("hashing")["algorithm"],
                    "OK" if h.stable else "ERROR",
                    h.error,
                ),
            )
            if h.digest and h.stable:
                by_hash[h.digest].append(r)
        for digest, group in by_hash.items():
            if len(group) < 2:
                continue
            canonical = min(
                group,
                key=lambda x: (
                    len(Path(x["absolute_path"]).parts),
                    str(x["absolute_path"]).casefold(),
                ),
            )
            cur = database.connect().execute(
                "INSERT INTO exact_duplicate_groups(full_hash,size_bytes,member_count,canonical_entry_id,canonical_selection_reason,verified) VALUES(?,?,?,?,?,1)",
                (
                    digest,
                    size,
                    len(group),
                    canonical["id"],
                    "deterministic shortest path",
                ),
            )
            gid = cur.lastrowid
            database.connect().executemany(
                "INSERT INTO exact_duplicate_members(group_id,entry_id,is_canonical,readable) VALUES(?,?,?,1)",
                [(gid, r["id"], int(r["id"] == canonical["id"])) for r in group],
            )
    database.connect().commit()
