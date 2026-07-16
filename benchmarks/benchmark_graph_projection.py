import argparse
from pathlib import Path
from housekeeper.database import Database
from housekeeper.graph.builder import build_projection

parser = argparse.ArgumentParser()
parser.add_argument("database", type=Path)
args = parser.parse_args()
db = Database(args.database)
db.initialize()
graph = build_projection(db, "universe", max_nodes=500, max_edges=2000)
print({"nodes": len(graph["nodes"]), "edges": len(graph["edges"]), "truncated": graph["truncated"]})
