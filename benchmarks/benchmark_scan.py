"""Small synthetic traversal benchmark; this never touches a mounted drive."""

import argparse
import time
from pathlib import Path

from housekeeper.database import Database
from housekeeper.config import load_config
from housekeeper.scanner import DriveScanner

parser = argparse.ArgumentParser()
parser.add_argument("fixture", type=Path)
args = parser.parse_args()
start = time.perf_counter()
config = load_config(workspace_override=Path("/tmp/housekeeper-benchmark-workspace"))
db = Database(config.database_path)
db.initialize()
counts = DriveScanner(db, config).scan(args.fixture, incremental=False)
print({"seconds": round(time.perf_counter() - start, 4), **counts})
