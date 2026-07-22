"""Conservative tokenization and word-shingling for prose."""

from __future__ import annotations

import re
import unicodedata

_TOKEN = re.compile(r"\w+", re.UNICODE)


def tokenize(text: str) -> list[str]:
    normalized = unicodedata.normalize("NFKC", text).casefold()
    return _TOKEN.findall(normalized)


def word_shingles(tokens: list[str], size: int = 5) -> set[str]:
    """Return the set of word shingles; short texts fall back to single tokens."""
    if size < 1:
        raise ValueError("shingle size must be positive")
    if len(tokens) < size:
        return set(tokens)
    return {" ".join(tokens[i : i + size]) for i in range(len(tokens) - size + 1)}


def exact_jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 1.0
    union = len(a | b)
    return len(a & b) / union if union else 0.0
