"""Small synthetic traversal benchmark; this never touches a mounted drive."""

import argparse
import tempfile
import time
from pathlib import Path

from housekeeper.config import load_config
from housekeeper.database import Database
from housekeeper.scanner import DriveScanner

parser = argparse.ArgumentParser()
parser.add_argument("fixture", type=Path)
# A fixed workspace accumulates scan history across runs, so the second run measures incremental
# reuse while claiming to measure a fresh scan. Isolate by default; --workspace opts back in.
parser.add_argument("--workspace", type=Path, default=None)
args = parser.parse_args()
start = time.perf_counter()
workspace = args.workspace or Path(tempfile.mkdtemp(prefix="housekeeper-benchmark-"))
config = load_config(workspace_override=workspace)
db = Database(config.database_path)
db.initialize()
counts = DriveScanner(db, config).scan(args.fixture, incremental=False)
print({"seconds": round(time.perf_counter() - start, 4), "workspace": str(workspace), **counts})
