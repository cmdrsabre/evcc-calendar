"""Car/train classification from title, description and rule list."""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass

from .models import Classification, Mode

# Keywords that force a mode; both languages are always recognised.
CAR_WORDS = {"auto", "car"}
TRAIN_WORDS = {"bahn", "train"}
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


def words(text: str) -> list:
    """Lowercase whole words without punctuation."""
    return [w.lower() for w in _WORD.findall(text or "")]


@dataclass(frozen=True)
class Rule:
    match: str
    mode: Mode


def _rule_hits(pattern: str, title_words: list) -> bool:
    # Split the pattern into words; '*' is kept as a wildcard.
    parts = [p for p in re.split(r"\s+", pattern.strip().lower()) if p]
    if not parts:
        return False
    n = len(parts)
    for i in range(len(title_words) - n + 1):
        if all(fnmatch.fnmatchcase(title_words[i + j], parts[j]) for j in range(n)):
            return True
    return False


def classify(title: str, description: str, rules: list) -> Classification:
    kw = set(words(title)) | set(words(description))
    has_auto, has_bahn = bool(kw & CAR_WORDS), bool(kw & TRAIN_WORDS)
    if has_auto and has_bahn:
        return Classification(Mode.AUTO, True, "both")
    if has_auto:
        return Classification(Mode.AUTO, False, "keyword_car", True)
    if has_bahn:
        return Classification(Mode.BAHN, False, "keyword_train", True)
    tw = words(title)
    for r in rules:
        if _rule_hits(r.match, tw):
            return Classification(r.mode, False, "rule", reason_arg=r.match)
    return Classification(Mode.AUTO, False, "default")
