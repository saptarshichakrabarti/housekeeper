import argparse
from pathlib import Path

from housekeeper.database import Database
from housekeeper.manifests import (load_manifest,
                                   validate_manifest_against_database,
                                   validate_manifest_schema)

p = argparse.ArgumentParser()
p.add_argument("manifest", type=Path)
p.add_argument("--database", type=Path, default=Path("workspace/inventory.sqlite"))
a = p.parse_args()
d = Database(a.database)
d.initialize()
e = load_manifest(a.manifest)
errors = validate_manifest_schema(e) + validate_manifest_against_database(e, d)
print("valid" if not errors else "\n".join(errors))
raise SystemExit(bool(errors))
