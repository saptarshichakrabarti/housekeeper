"""Generate synthetic metadata files for local benchmarks only."""

import argparse
from pathlib import Path

parser = argparse.ArgumentParser()
parser.add_argument("output", type=Path)
parser.add_argument("--files", type=int, default=1000)
args = parser.parse_args()
args.output.mkdir(parents=True, exist_ok=True)
for i in range(max(0, args.files)):
    (args.output / f"item-{i:08d}.dat").write_bytes(b"synthetic\n")
print(args.output)
