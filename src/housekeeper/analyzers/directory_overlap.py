from ..config import AppConfig
from ..database import Database


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


def build_directory_summaries(database: Database, config: AppConfig) -> None:
    return None


def run_directory_overlap_analysis(database: Database, config: AppConfig) -> None:
    return None
