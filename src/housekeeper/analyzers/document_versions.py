import re
from difflib import SequenceMatcher


def extract_version_tokens(filename: str) -> list[str]:
    return re.findall(
        r"(?:final|draft|revised|copy|backup|v\d+|\(\d+\))", filename.lower()
    )


def normalize_version_filename(filename: str) -> str:
    return re.sub(
        r"(?:[_ -]*(?:final|draft|revised|copy|backup|v\d+|\(\d+\)))+",
        "",
        filename.lower(),
    ).strip(" ._-")


def calculate_filename_similarity(a: str, b: str) -> float:
    return SequenceMatcher(
        None, normalize_version_filename(a), normalize_version_filename(b)
    ).ratio()


def run_document_version_analysis(database, config):
    return None
