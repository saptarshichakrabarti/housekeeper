"""Create a deterministic synthetic drive tree for testing.

Writes only under ``--output``. Optional-parser fixtures (docx/pdf/images) are produced
when those libraries import; otherwise skipped.
"""

import argparse
import os
import shutil
import struct
import tarfile
import zipfile
from pathlib import Path

# A fixed "old" timestamp (2016-01-01) so age-based rules are deterministic.
OLD_MTIME = 1_451_606_400.0


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _make_documents(root: Path) -> None:
    docs = root / "Documents"
    docs.mkdir(parents=True, exist_ok=True)
    (docs / "report.txt").write_text("historical report\n", encoding="utf-8")
    # Exact duplicate under a different name.
    shutil.copy2(docs / "report.txt", docs / "report (1).txt")
    # Same size, different content (defeats a size-only or quick-hash-only comparison).
    (docs / "notes.txt").write_text("historical report\n".replace("h", "H", 1), encoding="utf-8")
    # An Office temporary owner-lock file with a matching document nearby.
    (docs / "budget.docx").write_bytes(b"PK\x03\x04 fake docx body")
    (docs / "~$budget.docx").write_bytes(b"lock")
    # Unicode / unusual filenames.
    (docs / "résúmé — final(2).txt").write_text("unicode name", encoding="utf-8")


def _make_versions(root: Path) -> None:
    versions = root / "Versions"
    versions.mkdir(parents=True, exist_ok=True)
    try:
        from docx import Document  # type: ignore[import-not-found]
    except ImportError:
        (versions / "thesis_draft.txt").write_text("chapter one draft", encoding="utf-8")
        (versions / "thesis_final.txt").write_text("chapter one final revised", encoding="utf-8")
        return
    for name, body in (
        ("thesis_draft.docx", "Chapter one. Early draft."),
        ("thesis_v2.docx", "Chapter one. Second revision with more detail."),
        ("thesis_final.docx", "Chapter one. Final revised submission with more detail."),
    ):
        document = Document()
        document.add_paragraph(body)
        document.save(versions / name)


def _make_images(root: Path) -> None:
    photos = root / "Photos"
    photos.mkdir(parents=True, exist_ok=True)
    try:
        from PIL import Image  # type: ignore[import-not-found]
    except ImportError:
        _write(photos / "placeholder.bin", b"no Pillow available")
        return
    original = Image.new("RGB", (128, 128), (10, 120, 200))
    for x in range(128):
        for y in range(0, 128, 4):
            original.putpixel((x, y), (x % 256, y % 256, 90))
    original.save(photos / "sunset.png")
    # An exact duplicate and a resized (perceptually similar) copy.
    shutil.copy2(photos / "sunset.png", photos / "sunset-copy.png")
    original.resize((64, 64)).save(photos / "sunset-thumb.png")
    # A visually distinct image.
    Image.new("RGB", (128, 128), (240, 20, 20)).save(photos / "unrelated.png")


def _make_projects(root: Path) -> None:
    py = root / "Project"
    (py / ".git").mkdir(parents=True, exist_ok=True)
    (py / ".git" / "config").write_text("[core]\n", encoding="utf-8")
    (py / "pyproject.toml").write_text("[project]\nname='fixture'\n", encoding="utf-8")
    (py / "uv.lock").write_text("# lock", encoding="utf-8")
    (py / "src").mkdir(exist_ok=True)
    (py / "src" / "main.py").write_text("print('hello')\n", encoding="utf-8")
    (py / "__pycache__").mkdir(exist_ok=True)
    (py / "__pycache__" / "main.pyc").write_bytes(b"\x00cached bytecode")
    (py / ".venv" / "lib").mkdir(parents=True, exist_ok=True)
    (py / ".venv" / "lib" / "site.py").write_text("environment file\n", encoding="utf-8")

    js = root / "WebApp"
    js.mkdir(parents=True, exist_ok=True)
    (js / "package.json").write_text('{"name":"webapp"}\n', encoding="utf-8")
    (js / "package-lock.json").write_text('{"lockfileVersion":3}\n', encoding="utf-8")
    (js / "index.js").write_text("console.log('hi')\n", encoding="utf-8")
    (js / "node_modules" / "left-pad").mkdir(parents=True, exist_ok=True)
    (js / "node_modules" / "left-pad" / "index.js").write_text("module.exports=1\n", encoding="utf-8")


def _make_backups(root: Path) -> None:
    # A newer, more complete backup that fully contains the older one, plus unique historical data.
    newer = root / "Backups" / "laptop-2020"
    older = root / "Backups" / "laptop-2018"
    for base in (newer, older):
        (base / "docs").mkdir(parents=True, exist_ok=True)
        (base / "docs" / "a.txt").write_text("shared content a", encoding="utf-8")
        (base / "docs" / "b.txt").write_text("shared content b", encoding="utf-8")
    (newer / "docs" / "c.txt").write_text("new-only content c", encoding="utf-8")
    unique = older / "docs" / "old-only.txt"
    unique.write_text("irreplaceable historical note", encoding="utf-8")
    os.utime(unique, (OLD_MTIME, OLD_MTIME))


def _make_archives(root: Path) -> None:
    archives = root / "Archives"
    archives.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archives / "backup.zip", "w") as archive:
        archive.writestr("inside/report.txt", "archive member body")
        archive.writestr("inside/data.bin", b"\x00\x01\x02\x03")
    with tarfile.open(archives / "backup.tar.gz", "w:gz") as archive:
        import io

        payload = b"tar member body"
        info = tarfile.TarInfo("inside/notes.txt")
        info.size = len(payload)
        archive.addfile(info, io.BytesIO(payload))
    # A malformed archive: ZIP magic but truncated body.
    _write(archives / "corrupt.zip", b"PK\x03\x04 truncated and invalid")
    # A path-traversal ZIP (analysers must flag, never extract).
    with zipfile.ZipFile(archives / "traversal.zip", "w") as archive:
        archive.writestr("../escape.txt", "should never be extracted")


def _make_protected_and_unknown(root: Path) -> None:
    secrets = root / "Secrets"
    secrets.mkdir(parents=True, exist_ok=True)
    (secrets / "id_rsa").write_text("-----BEGIN PRIVATE KEY-----\n", encoding="utf-8")
    (secrets / "server.pem").write_text("-----BEGIN CERTIFICATE-----\n", encoding="utf-8")
    (secrets / "vault.kdbx").write_bytes(b"\x03\xd9\xa2\x9a keepass header")
    (root / "notes.sqlite").write_bytes(b"SQLite format 3\x00")
    # Unknown binary file (no recognisable signature).
    _write(root / "unknown.bin", bytes(range(256)))
    # An old installer that is byte-duplicated (defeats "old-only" removal, requires a duplicate).
    installer = root / "installers" / "setup-1.0.exe"
    _write(installer, b"MZ fake installer payload " + struct.pack("<I", 42))
    dup = root / "installers" / "old" / "setup-1.0-copy.exe"
    _write(dup, installer.read_bytes())
    for path in (installer, dup):
        os.utime(path, (OLD_MTIME, OLD_MTIME))


def _make_symlink_cycle(root: Path) -> None:
    links = root / "Links"
    links.mkdir(parents=True, exist_ok=True)
    try:
        (links / "self").symlink_to(links, target_is_directory=True)
        (links / "to_docs").symlink_to(root / "Documents", target_is_directory=True)
    except (OSError, NotImplementedError):
        (links / "no_symlink_support.txt").write_text("symlinks unavailable", encoding="utf-8")


def build_fixture(output: Path, clean: bool = False) -> Path:
    root = output.resolve()
    if clean and root.exists():
        shutil.rmtree(root)
    root.mkdir(parents=True, exist_ok=True)
    _make_documents(root)
    _make_versions(root)
    _make_images(root)
    _make_projects(root)
    _make_backups(root)
    _make_archives(root)
    _make_protected_and_unknown(root)
    _make_symlink_cycle(root)
    return root


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--clean", action="store_true")
    args = parser.parse_args()
    print(build_fixture(args.output, args.clean))


if __name__ == "__main__":
    main()
