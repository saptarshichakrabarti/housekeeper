import argparse
import sqlite3
from pathlib import Path

p = argparse.ArgumentParser()
p.add_argument(
    "database", type=Path, nargs="?", default=Path("workspace/inventory.sqlite")
)
a = p.parse_args()
c = sqlite3.connect(a.database)
for table in (
    "scan_runs",
    "filesystem_entries",
    "file_signatures",
    "exact_duplicate_groups",
    "classifications",
    "move_transactions",
):
    print(table, c.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
