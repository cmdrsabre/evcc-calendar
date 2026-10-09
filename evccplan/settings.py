"""Settings that can be changed in the web UI. They overlay config.yaml and are stored in the database.

Connection data and secrets (URLs, tokens, keys) are never editable here.
"""
from __future__ import annotations

import re
from typing import Optional

from . import i18n
from .i18n import t
from .classify import Rule
from .models import Mode

# name -> (type, minimum, maximum)
FIELDS = {
    "warm": (float, 5, 60), "cold": (float, 5, 60), "temp_threshold_c": (float, -20, 30),
    "reserve_soc": (float, 0, 50), "unclear_cap_soc": (int, 5, 100),
    "min_car_km": (float, 0, 50), "manual_drive_min": (float, 5, 600),
    "time_buffer_min": (float, 0, 240), "chain_window_h": (float, 0, 48),
    "charge_power_kw": (float, 1, 50), "charge_loss_margin": (float, 0, 1),
    "vehicle_limit_min": (int, 0, 100),
}
MAX_RULES = 50
_PATTERN = re.compile(r"^[\w*\- ]{1,40}$", re.UNICODE)


def _number(name: str, value, lang: str = "en"):
    kind, lo, hi = FIELDS[name]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(t(lang, "api.set_number_expected", name=name))
    if kind is int and value != int(value):
        raise ValueError(t(lang, "api.set_int_expected", name=name))
    if not lo <= value <= hi:
        raise ValueError(t(lang, "api.set_range", name=name, lo="%g" % lo, hi="%g" % hi))
    return kind(value)


def _rules(value, lang: str = "en") -> list:
    if not isinstance(value, list) or len(value) > MAX_RULES:
        raise ValueError(t(lang, "api.set_rules_list", max=MAX_RULES))
    out = []
    for r in value:
        if not isinstance(r, dict) or not isinstance(r.get("match"), str):
            raise ValueError(t(lang, "api.set_rules_entry"))
        match = " ".join(r["match"].split())
        if not _PATTERN.match(match) or not match.replace("*", "").strip():
            raise ValueError(t(lang, "api.set_rules_match", match=r["match"][:40]))
        if r.get("mode") not in ("auto", "bahn"):
            raise ValueError(t(lang, "api.set_rules_mode"))
        out.append({"match": match, "mode": r["mode"]})
    return out


def validate(data, lang: str = "en") -> dict:
    """Checks a (partial) settings dict. Raises ValueError with a readable message."""
    if not isinstance(data, dict):
        raise ValueError(t(lang, "api.invalid_values"))
    out = {}
    for k, v in data.items():
        if k == "rules":
            out[k] = _rules(v, lang)
        elif k == "language":
            if v not in i18n.available():
                raise ValueError(t(lang, "api.set_language_unknown"))
            out[k] = v
        elif k in FIELDS:
            out[k] = _number(k, v, lang)
        else:
            raise ValueError(t(lang, "api.set_unknown", name=str(k)[:40]))
    return out


class Settings:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        self.base = self._snapshot()
        try:
            self._apply(validate(store.get("settings") or {}))
        except ValueError:
            pass                                    # damaged stored values: fall back to config.yaml

    def _snapshot(self) -> dict:
        snap = {k: getattr(self.cfg, k) for k in FIELDS}
        snap["language"] = self.cfg.language
        snap["rules"] = [{"match": r.match, "mode": r.mode.value} for r in self.cfg.rules]
        return snap

    def _apply(self, values: dict) -> None:
        for k, v in values.items():
            if k == "rules":
                self.cfg.rules = [Rule(r["match"], Mode(r["mode"])) for r in v]
            elif k == "language":
                self.cfg.language = v
            else:
                setattr(self.cfg, k, v)

    def values(self) -> dict:
        return self._snapshot()

    def changed(self) -> list:
        return sorted(k for k, v in self.values().items() if v != self.base[k])

    def update(self, data: dict) -> None:
        clean = validate(data, self.cfg.language)
        stored = dict(self.store.get("settings") or {})
        stored.update(clean)
        self.store.put("settings", stored)
        self._apply(clean)

    def reset(self) -> None:
        self.store.delete("settings")
        self._apply(self.base)

    def limits(self) -> dict:
        return {k: {"min": lo, "max": hi, "int": kind is int} for k, (kind, lo, hi) in FIELDS.items()}
