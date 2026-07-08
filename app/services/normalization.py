from __future__ import annotations

import re
import unicodedata

_WORD_RE = re.compile(r"[^a-z0-9\s]")
_SPACE_RE = re.compile(r"\s+")


def normalize_text(value: str) -> str:
    """Normalize player input and category data for matching."""
    value = unicodedata.normalize("NFKD", value or "")
    value = value.encode("ascii", "ignore").decode("ascii")
    value = value.lower().strip()
    value = value.replace("&", " and ")
    value = _WORD_RE.sub(" ", value)
    value = _SPACE_RE.sub(" ", value).strip()
    return value


def singularize_phrase(value: str) -> str:
    """Small, dependency-free plural handling.

    This intentionally stays conservative. Category aliases should cover weird nouns.
    """
    words = normalize_text(value).split()
    if not words:
        return ""
    words[-1] = singularize_word(words[-1])
    return " ".join(words)


def singularize_word(word: str) -> str:
    if len(word) <= 3:
        return word
    irregular = {
        "men": "man",
        "women": "woman",
        "children": "child",
        "people": "person",
        "mice": "mouse",
        "geese": "goose",
        "teeth": "tooth",
        "feet": "foot",
    }
    if word in irregular:
        return irregular[word]
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith("ves") and len(word) > 4:
        return word[:-3] + "f"
    if word.endswith("es") and (
        word.endswith("ses")
        or word.endswith("xes")
        or word.endswith("zes")
        or word.endswith("ches")
        or word.endswith("shes")
    ):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word
