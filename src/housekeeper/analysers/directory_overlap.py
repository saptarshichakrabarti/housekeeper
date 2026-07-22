from ..config import AppConfig
import json
from ..database import Database
from ..relationships import upsert_relationship
from .scope import AnalyzerScope, scoped_entry_ids


def calculate_containment(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a) if a else 0.0


def calculate_jaccard(a: set[str], b: set[str]) -> float:
    return len(a & b) / len(a | b) if a | b else 1.0


def get_directory_hash_set(directory_id: int, database: Database) -> set[str]:
    r = database.fetch_one(
        "SELECT relative_path FROM filesystem_entries WHERE id=?", (directory_id,)
    )
    if not r:
        return set()
    return {
        x["full_hash"]
        for x in database.fetch_all(
            "SELECT s.full_hash FROM filesystem_entries e JOIN file_signatures s ON s.entry_id=e.id WHERE e.relative_path LIKE ? AND s.full_hash IS NOT NULL",
            (r["relative_path"] + "/%",),
        )
    }


def build_directory_summaries(
    database: Database, config: AppConfig, scope: AnalyzerScope | None = None
) -> None:
    database.connect().execute("DELETE FROM directory_summaries")
    dirs = database.fetch_all(
        "SELECT id,scan_run_id,relative_path FROM filesystem_entries WHERE entry_type='directory'"
    )
    allowed = scoped_entry_ids(database, scope, "directory") if scope else None
    for directory in dirs:
        if allowed is not None and int(directory["id"]) not in allowed:
            continue
        prefix = directory["relative_path"] + "/%" if directory["relative_path"] else "%"
        files = database.fetch_all(
            """SELECT e.size_bytes,e.modified_at,s.full_hash,e.suffix FROM filesystem_entries e
            LEFT JOIN file_signatures s ON s.entry_id=e.id WHERE e.scan_run_id=? AND e.entry_type='file' AND e.relative_path LIKE ?""",
            (directory["scan_run_id"], prefix),
        )
        hashes = {x["full_hash"] for x in files if x["full_hash"]}
        ext: dict[str, int] = {}
        for item in files:
            ext[item["suffix"] or ""] = ext.get(item["suffix"] or "", 0) + 1
        database.connect().execute(
            """INSERT INTO directory_summaries(entry_id,recursive_file_count,recursive_directory_count,recursive_size_bytes,unique_full_hash_count,duplicate_file_count,extension_distribution_json,earliest_modified_at,latest_modified_at,content_signature) VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (
                directory["id"],
                len(files),
                0,
                sum(x["size_bytes"] or 0 for x in files),
                len(hashes),
                len(files) - len(hashes),
                json.dumps(ext, sort_keys=True),
                min(
                    (x["modified_at"] for x in files if x["modified_at"] is not None), default=None
                ),
                max(
                    (x["modified_at"] for x in files if x["modified_at"] is not None), default=None
                ),
                __import__("hashlib").sha256("|".join(sorted(hashes)).encode()).hexdigest(),
            ),
        )
    database.connect().commit()


# A single hash shared by more directories than this is treated as non-discriminating and
# skipped for candidate generation to keep the pairwise fan-out bounded; any real backup
# relationship is still found through its more distinctive shared content.
MAX_SHARED_HASH_FANOUT = 64


def _candidate_directory_hash_sets(
    database: Database, config: AppConfig
) -> dict[int, set[str]]:
    """Fetch each candidate directory's verified-hash set exactly once (N queries, not N²)."""
    rows = database.fetch_all(
        """SELECT ds.entry_id,e.scan_run_id,e.relative_path
           FROM directory_summaries ds JOIN filesystem_entries e ON e.id=ds.entry_id
           WHERE ds.recursive_file_count>=?""",
        (config.section("directory_overlap")["minimum_files"],),
    )
    result: dict[int, set[str]] = {}
    for row in rows:
        prefix = (row["relative_path"] + "/%") if row["relative_path"] else "%"
        hashes = {
            r["full_hash"]
            for r in database.iter_rows(
                "SELECT s.full_hash FROM filesystem_entries e JOIN file_signatures s ON s.entry_id=e.id "
                "WHERE e.scan_run_id=? AND e.relative_path LIKE ? AND s.full_hash IS NOT NULL",
                (row["scan_run_id"], prefix),
            )
        }
        if hashes:
            result[int(row["entry_id"])] = hashes
    return result


def generate_candidate_directory_pairs(
    dir_hashes: dict[int, set[str]],
) -> set[tuple[int, int]]:
    """Only directories that share at least one verified content hash are compared."""
    from collections import defaultdict

    hash_to_dirs: dict[str, list[int]] = defaultdict(list)
    for directory_id, hashes in dir_hashes.items():
        for content_hash in hashes:
            hash_to_dirs[content_hash].append(directory_id)
    pairs: set[tuple[int, int]] = set()
    for sharing in hash_to_dirs.values():
        if not 2 <= len(sharing) <= MAX_SHARED_HASH_FANOUT:
            continue
        ordered = sorted(sharing)
        for i, left in enumerate(ordered):
            for right in ordered[i + 1 :]:
                pairs.add((left, right))
    return pairs


def run_directory_overlap_analysis(
    database: Database,
    config: AppConfig,
    scope: AnalyzerScope | None = None,
    job_id: int | None = None,
) -> None:
    from ..jobs import checkpoint

    build_directory_summaries(database, config, scope)
    threshold = config.section("directory_overlap")["containment_threshold"]
    dir_hashes = _candidate_directory_hash_sets(database, config)
    for index, (a_id, b_id) in enumerate(sorted(generate_candidate_directory_pairs(dir_hashes)), 1):
        checkpoint(database, job_id, processed_count=index, state={"last_pair": [a_id, b_id]})
        hashes_a, hashes_b = dir_hashes[a_id], dir_hashes[b_id]
        containment = max(
            calculate_containment(hashes_a, hashes_b), calculate_containment(hashes_b, hashes_a)
        )
        if containment >= threshold:
            # The smaller (more likely contained/predecessor) directory is the source.
            source_id, target_id = (a_id, b_id) if len(hashes_a) <= len(hashes_b) else (b_id, a_id)
            upsert_relationship(
                database,
                "DIRECTORY",
                source_id,
                "DIRECTORY",
                target_id,
                "MOSTLY_CONTAINED_IN",
                containment,
                {
                    "shared_hashes": len(hashes_a & hashes_b),
                    "source_hashes": len(dir_hashes[source_id]),
                    "target_hashes": len(dir_hashes[target_id]),
                },
                "1",
            )
