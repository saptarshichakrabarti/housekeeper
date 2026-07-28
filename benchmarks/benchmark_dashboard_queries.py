import argparse
import time
from pathlib import Path

from housekeeper.database import Database

parser = argparse.ArgumentParser()
parser.add_argument("database", type=Path)
args = parser.parse_args()
db = Database(args.database)
db.initialize()
start = time.perf_counter()
rows = db.fetch_all(
    "SELECT e.id,e.relative_path FROM filesystem_entries e WHERE e.id>? ORDER BY e.id LIMIT ?",
    (0, 100),
)
print({"seconds": time.perf_counter() - start, "rows": len(rows), "keyset": True})
