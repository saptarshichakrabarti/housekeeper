import argparse
from pathlib import Path
from housekeeper.config import load_config
from housekeeper.database import Database
from housekeeper.analysers.directory_overlap import run_directory_overlap_analysis

parser = argparse.ArgumentParser()
parser.add_argument("database", type=Path)
args = parser.parse_args()
config = load_config(workspace_override=args.database.parent)
db = Database(args.database)
db.initialize()
run_directory_overlap_analysis(db, config)
print({"relationships": db.fetch_one("SELECT COUNT(*) AS n FROM relationships")["n"]})
