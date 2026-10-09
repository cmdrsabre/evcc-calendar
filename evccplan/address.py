"""Pre-filter: extracts a geocodable address from free-text location strings."""
from __future__ import annotations

import re

_SUFFIX = re.compile(
    r"(stra(ß|ss)e|str\.|weg|allee|platz|damm|ring|chaussee|ufer|gasse|steig|zeile|pfad|promenade|brücke)$",
    re.IGNORECASE,
)
_PLZ_CITY = re.compile(r"\b(\d{5})\s+([A-ZÄÖÜ][^\s,]*)")
_NUMBER_END = re.compile(r"(\d+\s?[a-zA-Z]?(?:\s?[-–/]\s?\d+\s?[a-zA-Z]?)?)$")


def _normalize(text: str) -> str:
    s = re.sub(r"\s*[\r\n]+\s*", ", ", (text or "").strip())
    return re.sub(r"\s+", " ", s)


def _street_from(prefix: str) -> str:
    """Last segment before the house number; street name starts at the first street word."""
    segment = re.split(r",| - | – ", prefix)[-1].strip()
    tokens = segment.split()
    if not tokens:
        return ""
    window = tokens[-4:]
    for i, tok in enumerate(window):
        if _SUFFIX.search(tok.strip(",")):
            return " ".join(window[i:])
    return " ".join(window)


def candidates(location: str) -> list:
    """Geocoding queries, best first. Empty list if there is no location."""
    raw = _normalize(location)
    if not raw:
        return []
    out = []
    m = _PLZ_CITY.search(raw)
    if m and m.start() > 0:
        city = m.group(2).split("-Bezirk")[0].strip("-")
        before = raw[: m.start()].rstrip(" ,")
        nm = _NUMBER_END.search(before)
        if nm:
            street = _street_from(before[: nm.start()].rstrip(" ,"))
            number = re.sub(r"\s+", "", nm.group(1))
            if street:
                out.append("%s %s, %s %s" % (street, number, m.group(1), city))
    elif not m:
        nm = _NUMBER_END.search(raw)
        if nm:
            street = _street_from(raw[: nm.start()].rstrip(" ,"))
            if street and _SUFFIX.search(street.split()[-1] if street.split() else ""):
                out.append("%s %s" % (street, re.sub(r"\s+", "", nm.group(1))))
    if raw not in out:
        out.append(raw)
    return out
