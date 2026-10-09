import json
import re
from pathlib import Path

from evccplan import i18n

ROOT = Path(__file__).resolve().parent.parent / "evccplan"
PLACEHOLDER = re.compile(r"\{(\w+)\}")


def test_all_languages_have_the_same_keys_and_placeholders():
    langs = i18n.available()
    assert "en" in langs and "de" in langs
    base = i18n.load("en")
    for lang in langs:
        other = i18n.load(lang)
        assert set(other) == set(base), (lang, set(other) ^ set(base))
        for key, text in base.items():
            assert set(PLACEHOLDER.findall(text)) == set(PLACEHOLDER.findall(other[key])), (lang, key)


def test_every_key_used_in_the_code_exists():
    keys = set(i18n.load("en"))
    used = set()
    for f in list(ROOT.glob("*.py")) + [ROOT / "static" / "app.js", ROOT / "static" / "index.html"]:
        text = f.read_text(encoding="utf-8")
        used |= set(re.findall(r"""["']((?:ui|api|planner|runner)\.[a-z0-9_.]+)["']""", text))
        used |= set(re.findall(r"""data-i18n[\w-]*=["']((?:ui|api|planner|runner)\.[a-z0-9_.]+)["']""", text))
    missing = sorted(k for k in used if k not in keys and not any(x.startswith(k) for x in keys))
    assert not missing, missing


def test_fallback_to_english_and_to_key():
    assert i18n.t("xx", "api.unknown_key_zzz") == "api.unknown_key_zzz"
    some = next(iter(i18n.load("en")))
    assert i18n.t("xx", some) == i18n.load("en")[some] or "{" in i18n.load("en")[some]


def test_locale_files_are_valid_json_with_string_values():
    for lang in i18n.available():
        d = json.loads((i18n.LOCALES / ("%s.json" % lang)).read_text(encoding="utf-8"))
        assert all(isinstance(v, str) for v in d.values())
