"""Create a small deterministic synthetic drive tree."""

import argparse
import shutil
import zipfile
from pathlib import Path


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--clean", action="store_true")
    a = p.parse_args()
    root = a.output.resolve()
    if a.clean and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    (root / "Documents").mkdir()
    (root / "Documents" / "report.txt").write_text(
        "historical report\n", encoding="utf-8"
    )
    shutil.copy2(
        root / "Documents" / "report.txt", root / "Documents" / "report (1).txt"
    )
    (root / "Project" / ".git").mkdir(parents=True)
    (root / "Project" / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    (root / "Project" / "__pycache__").mkdir()
    (root / "Project" / "__pycache__" / "x.pyc").write_bytes(b"cache")
    (root / "backup-old").mkdir()
    (root / "backup-old" / "unique.bin").write_bytes(b"unique historical data")
    with zipfile.ZipFile(root / "archive.zip", "w") as z:
        z.writestr("inside.txt", "archive member")
    (root / "unknown.bin").write_bytes(bytes(range(256)))
    print(root)


if __name__ == "__main__":
    main()
