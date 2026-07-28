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
rows = db.fetch_all("SELECT id FROM filesystem_entries ORDER BY id LIMIT 1000")
elapsed = time.perf_counter() - start
print({"seconds": elapsed, "rows": len(rows), "first_page": elapsed < 1.0, **db.database_stats()})
