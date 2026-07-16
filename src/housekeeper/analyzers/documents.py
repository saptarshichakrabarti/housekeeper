import hashlib
import re
import unicodedata
from pathlib import Path


def normalize_document_text(text: str, max_chars: int) -> str:
    return re.sub(
        r"\s+", " ", unicodedata.normalize("NFKC", text.replace("\x00", " "))
    ).strip()[:max_chars]


def compute_normalized_text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def extract_plaintext_metadata(path: Path, config):
    try:
        text = normalize_document_text(
            path.read_text(errors="replace"),
            config.section("documents")["max_text_characters"],
        )
        return {
            "character_count": len(text),
            "word_count": len(text.split()),
            "normalized_text_hash": compute_normalized_text_hash(text),
            "extraction_status": "OK",
        }
    except OSError as exc:
        return {"extraction_status": "ERROR", "extraction_error": str(exc)}


def extract_document(path: Path, file_type: str, config):
    return (
        extract_plaintext_metadata(path, config)
        if file_type in {"txt", "md", "csv", "text"}
        else {"extraction_status": "UNSUPPORTED"}
    )


def run_document_analysis(database, config):
    return None
