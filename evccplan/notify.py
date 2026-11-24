"""Notification routing: which kind of notice goes to which Home Assistant notify entity, plus quiet hours."""
from __future__ import annotations

import re
from datetime import datetime
from zoneinfo import ZoneInfo

from .i18n import t

CATEGORIES = ["plan", "plan_warn", "late", "unplugged", "carlimit", "outage"]
# notice key prefix (text before the first ':') -> category
PREFIX = {
    "set": "plan", "del": "plan",
    "addr": "plan_warn", "notarget": "plan_warn", "overlap": "plan_warn", "over100": "plan_warn",
    "unclear": "plan_warn", "tight": "plan_warn", "manual": "plan_warn", "noteffective": "plan_warn",
    "overrun": "late", "earlier": "late",
    "unplugged": "unplugged",
    "carlimit": "carlimit", "carlimit_ok": "carlimit", "carlimit_retry": "carlimit",
    "evcc_down": "outage", "outage": "outage", "ors_down": "outage", "calendar_down": "outage", "evccwrite": "outage",
}
LEVEL_OF = {"plan": "info", "plan_warn": "warning", "late": "warning", "unplugged": "warning",
            "carlimit": "warning", "outage": "error"}       # used to derive routes from YAML notify_targets
MAX_ENTITIES = 20
MAX_HELD = 30
_HHMM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")
DEFAULT_QUIET = {"enabled": False, "from": "22:00", "to": "07:00"}


def category(key: str, level: str) -> str:
    c = PREFIX.get(key.split(":", 1)[0])
    if c:
        return c
    return {"info": "plan", "error": "outage"}.get(level, "plan_warn")


def yaml_routes(notify_targets: dict) -> dict:
    return {c: list(notify_targets.get(LEVEL_OF[c]) or []) for c in CATEGORIES}


def validate(data, known: set, lang: str = "en") -> dict:
    """Checks {'routes': {cat: [entity]}, 'quiet': {...}, 'min_late_min': number}; every key is optional."""
    def bad(detail):
        return ValueError(t(lang, "api.notify_invalid", detail=detail))

    if not isinstance(data, dict):
        raise ValueError(t(lang, "api.invalid_values"))
    out = {}
    for k, v in data.items():
        if k == "routes":
            if not isinstance(v, dict):
                raise bad("routes")
            routes, distinct = {}, set()
            for cat, ents in v.items():
                if cat not in CATEGORIES:
                    raise bad(str(cat)[:40])
                if not isinstance(ents, list) or not all(isinstance(e, str) for e in ents):
                    raise bad(cat)
                for e in ents:
                    if not e.startswith("notify.") or e not in known:
                        raise bad(e[:60])
                routes[cat] = list(dict.fromkeys(ents))
                distinct.update(ents)
            if len(distinct) > MAX_ENTITIES:
                raise bad("max %d" % MAX_ENTITIES)
            out["routes"] = routes
        elif k == "quiet":
            if not isinstance(v, dict) or not isinstance(v.get("enabled"), bool):
                raise bad("quiet")
            for f in ("from", "to"):
                if not isinstance(v.get(f), str) or not _HHMM.match(v[f]):
                    raise bad("quiet." + f)
            out["quiet"] = {"enabled": v["enabled"], "from": v["from"], "to": v["to"]}
        elif k == "min_late_min":
            if isinstance(v, bool) or not isinstance(v, (int, float)) or not 0 <= v <= 60:
                raise bad("min_late_min")
            out["min_late_min"] = float(v)
        else:
            raise bad(str(k)[:40])
    return out


class Notifier:
    def __init__(self, cfg, store):
        self.cfg, self.store = cfg, store
        self.yaml_min_late = float(getattr(cfg, "notify_min_late_min", 2.0))
        v = self.prefs().get("min_late_min")
        if isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 60:
            cfg.notify_min_late_min = float(v)

    def prefs(self) -> dict:
        p = self.store.get("notify")
        return p if isinstance(p, dict) else {}

    def routes(self) -> dict:
        r = self.prefs().get("routes")
        return r if isinstance(r, dict) and r else yaml_routes(self.cfg.notify_targets)

    def quiet(self) -> dict:
        return self.prefs().get("quiet") or dict(DEFAULT_QUIET)

    def targets(self, notice) -> list:
        return list(self.routes().get(category(notice.key, notice.level)) or [])

    def in_quiet(self, now_utc: datetime) -> bool:
        q = self.prefs().get("quiet") or {}
        if not q.get("enabled"):
            return False
        try:
            local = now_utc.astimezone(ZoneInfo(self.cfg.timezone)).strftime("%H:%M")
        except Exception:
            local = now_utc.strftime("%H:%M")
        start, end = q.get("from", "22:00"), q.get("to", "07:00")
        if start == end:
            return False
        if start < end:
            return start <= local < end
        return local >= start or local < end                # window across midnight

    def hold(self, notice) -> None:
        held = [h for h in (self.store.get("notify_held") or []) if h.get("key") != notice.key]
        held.append({"key": notice.key, "text": notice.text, "level": notice.level, "ttl": notice.ttl_hours})
        self.store.put("notify_held", held[-MAX_HELD:])

    def take_held(self) -> list:
        held = self.store.get("notify_held") or []
        if held:
            self.store.delete("notify_held")
        return held

    def update(self, data, known: set) -> None:
        clean = validate(data, known, self.cfg.language)
        prefs = dict(self.prefs())
        prefs.update({k: v for k, v in clean.items() if k != "min_late_min"})
        if "min_late_min" in clean:
            prefs["min_late_min"] = clean["min_late_min"]
            self.cfg.notify_min_late_min = clean["min_late_min"]
        self.store.put("notify", prefs)

    def reset(self) -> None:
        self.store.delete("notify")
        self.cfg.notify_min_late_min = self.yaml_min_late
