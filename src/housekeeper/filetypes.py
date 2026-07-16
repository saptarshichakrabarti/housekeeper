import mimetypes
from pathlib import Path


def detect_by_extension(path: Path):
    return mimetypes.guess_type(path.name)


def detect_by_signature(path: Path):
    try:
        with path.open("rb") as f:
            head = f.read(4096)
        if head.startswith(b"%PDF"):
            return "application/pdf", "pdf"
        if head.startswith(b"PK\x03\x04"):
            return "application/zip", "zip-container"
        if head.startswith(b"\x89PNG"):
            return "image/png", "png"
        if head.startswith(b"\xff\xd8\xff"):
            return "image/jpeg", "jpeg"
    except OSError:
        pass
    return None, None


class FileSignature:
    def __init__(
        self,
        extension_mime=None,
        detected_mime=None,
        detected_type=None,
        source="unknown",
    ):
        self.extension_mime = extension_mime
        self.detected_mime = detected_mime
        self.detected_type = detected_type
        self.signature_source = source


def detect_file_type(path: Path) -> FileSignature:
    em, _ = detect_by_extension(path)
    dm, dt = detect_by_signature(path)
    return FileSignature(
        em,
        dm or em,
        dt or path.suffix.lower().lstrip("."),
        "signature" if dm else "extension",
    )


def classify_high_level_type(signature: FileSignature) -> str:
    m = signature.detected_mime or ""
    t = signature.detected_type or ""
    return (
        "image"
        if m.startswith("image/")
        else (
            "audio"
            if m.startswith("audio/")
            else (
                "document"
                if m.startswith("text/")
                or t in {"pdf", "docx", "xlsx", "pptx", "md", "csv"}
                else "archive" if t in {"zip", "tar", "gz", "bz2", "xz"} else "other"
            )
        )
    )


def is_supported_document(s):
    return classify_high_level_type(s) == "document"


def is_supported_archive(s):
    return classify_high_level_type(s) == "archive" or s.detected_type in {"zip", "tar"}


def is_supported_image(s):
    return classify_high_level_type(s) == "image"


def is_supported_audio(s):
    return classify_high_level_type(s) == "audio"
