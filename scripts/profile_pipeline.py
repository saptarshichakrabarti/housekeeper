import argparse
import json
import time
from pathlib import Path
from housekeeper.config import load_config
from housekeeper.database import Database
from housekeeper.scanner import DriveScanner

parser = argparse.ArgumentParser(); parser.add_argument("source", type=Path); parser.add_argument("--workspace", type=Path, default=Path("workspace")); args = parser.parse_args()
config = load_config(workspace_override=args.workspace); db = Database(config.database_path); db.initialize(); start = time.perf_counter(); counts = DriveScanner(db, config).scan(args.source, incremental=True); output = {"elapsed_seconds": time.perf_counter() - start, "counts": counts, "database": db.database_stats()}; (config.workspace / "reports").mkdir(parents=True, exist_ok=True); (config.workspace / "reports" / "profile.json").write_text(json.dumps(output, indent=2), encoding="utf-8"); print(output)
