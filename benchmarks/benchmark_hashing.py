import argparse
import time
from pathlib import Path
from housekeeper.hashing import compute_full_hash

parser = argparse.ArgumentParser()
parser.add_argument("fixture", type=Path)
args = parser.parse_args()
start = time.perf_counter()
result = compute_full_hash(args.fixture, "sha256", 8_388_608)
print(
    {
        "seconds": time.perf_counter() - start,
        "bytes": result.size,
        "bytes_per_second": result.size / max(1e-9, time.perf_counter() - start),
        "stable": result.stable,
    }
)
