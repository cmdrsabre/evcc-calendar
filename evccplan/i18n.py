"""Translations. One JSON file per language in evccplan/static/locales/ (shared by the server and the web UI).

Keys are flat strings like "planner.too_late". Placeholders use str.format syntax: "{soc} %".
A missing key falls back to English, then to the key itself. Log messages are not translated.
"""
from __future__ import annotations

import json
import threading
from pathlib import Path

LOCALES = Path(__file__).resolve().parent / "static" / "locales"
DEFAULT = "en"
_cache: dict = {}
_lock = threading.Lock()


def available() -> list:
    return sorted(p.stem for p in LOCALES.glob("*.json"))


def load(lang: str) -> dict:
    with _lock:
        if lang not in _cache:
            try:
                _cache[lang] = json.loads((LOCALES / ("%s.json" % lang)).read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _cache[lang] = {}
        return _cache[lang]


def t(lang: str, key: str, **kw) -> str:
    text = load(lang).get(key)
    if text is None:
        text = load(DEFAULT).get(key, key)
    try:
        return text.format(**kw) if kw else text
    except (KeyError, IndexError, ValueError):
        return text
