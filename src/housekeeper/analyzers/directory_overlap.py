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


def run_directory_overlap_analysis(
    database: Database, config: AppConfig, scope: AnalyzerScope | None = None
) -> None:
    build_directory_summaries(database, config, scope)
    rows = database.fetch_all(
        "SELECT entry_id,recursive_file_count,recursive_size_bytes,content_signature FROM directory_summaries WHERE recursive_file_count>=?",
        (config.section("directory_overlap")["minimum_files"],),
    )
    for a in rows:
        hashes_a = get_directory_hash_set(a["entry_id"], database)
        if not hashes_a:
            continue
        for b in rows:
            if a["entry_id"] >= b["entry_id"]:
                continue
            hashes_b = get_directory_hash_set(b["entry_id"], database)
            if not hashes_b:
                continue
            containment = max(
                calculate_containment(hashes_a, hashes_b), calculate_containment(hashes_b, hashes_a)
            )
            if containment >= config.section("directory_overlap")["containment_threshold"]:
                source, target = (a, b) if len(hashes_a) <= len(hashes_b) else (b, a)
                upsert_relationship(
                    database,
                    "DIRECTORY",
                    source["entry_id"],
                    "DIRECTORY",
                    target["entry_id"],
                    "MOSTLY_CONTAINED_IN",
                    containment,
                    {
                        "shared_hashes": len(hashes_a & hashes_b),
                        "source_hashes": len(hashes_a),
                        "target_hashes": len(hashes_b),
                    },
                    "1",
                )
