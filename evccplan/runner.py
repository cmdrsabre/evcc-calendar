"""One run: read calendar, plan, reconcile with evcc, send notices."""
from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from . import address, planner
from .clients import (ApiError, EvccClient, EvccDown, HAClient, OrsClient, OrsDown,
                      forecast_temperature, straight_line_route)
from .models import Event
from .config import Config
from .models import Notice, Route, override_key
from .store import Store

log = logging.getLogger("evccplan")
GOOD_LAYERS = ("address", "venue")


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


class Runner:
    def __init__(self, cfg: Config, store: Store, ha: HAClient, evcc: EvccClient, ors: Optional[OrsClient],
                 now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.cfg, self.store, self.ha, self.evcc, self.ors, self.now_fn = cfg, store, ha, evcc, ors, now_fn
        self._lock = threading.Lock()
        self._state: dict = {}
        self._last_ok: dict = {}
        self._ors_down = False

    # ------------------------------------------------------------ Addresses and routes
    def _retry(self, fn, what: str, down_cls=ApiError):
        """Up to `retries` attempts with growing pauses; then down_cls."""
        last = None
        for n in range(max(1, self.cfg.retries)):
            try:
                return fn()
            except ApiError as exc:
                last = exc
                log.warning("%s: Versuch %d/%d fehlgeschlagen (%s)", what, n + 1, self.cfg.retries, exc)
                if n < self.cfg.retries - 1 and self.cfg.retry_delay_s > 0:
                    time.sleep(self.cfg.retry_delay_s * (3 ** n))
        raise down_cls("%s antwortet nicht (nach %d Versuchen): %s" % (what, self.cfg.retries, last))

    def _ors_call(self, fn):
        if self._ors_down:
            raise OrsDown("OpenRouteService zuvor nicht erreichbar")
        try:
            return fn()
        except ApiError as exc:
            self._ors_down = True
            raise OrsDown(str(exc)) from None

    def _home(self) -> tuple:
        if self.cfg.home_known:
            return (self.cfg.home_lat, self.cfg.home_lon)
        cached = self.store.cache_get("home", 3650)
        if cached:
            return tuple(cached)
        if not self.ors or not self.cfg.home_address:
            raise ApiError("Heimatadresse fehlt")
        p = self._ors_call(lambda: self.ors.geocode(self.cfg.home_address))
        if not p:
            raise ApiError("Heimatadresse nicht gefunden")
        self.store.cache_put("home", [p.lat, p.lon])
        return (p.lat, p.lon)

    def _geocode(self, query: str):
        key = "geo:" + query.lower()
        hit = self.store.cache_get(key)
        if hit is not None:
            return hit or None
        p = self._ors_call(lambda: self.ors.geocode(query))
        self.store.cache_put(key, ({"lat": p.lat, "lon": p.lon, "label": p.label,
                                    "confidence": p.confidence, "layer": p.layer} if p else {}))
        return self.store.cache_get(key) or None

    def resolve(self, event) -> tuple:
        """-> (Route|None, error text|None, place name)"""
        if not self.ors:
            return None, "ORS-Key fehlt", ""
        best_note = ""
        try:
            home = self._home()
            for q in address.candidates(event.location):
                g = self._geocode(q)
                if not g:
                    continue
                if g["layer"] in GOOD_LAYERS and g["confidence"] >= self.cfg.ors_min_confidence:
                    return self._route(home, (g["lat"], g["lon"])), None, g["label"]
                best_note = best_note or "bester Treffer '%s' (Ebene %s, Sicherheit %.1f)" % (
                    g["label"], g["layer"], g["confidence"])
        except OrsDown as exc:
            return None, "ORS nicht erreichbar (%s)" % exc, ""
        return None, "Adresse nicht eindeutig gefunden" + (": " + best_note if best_note else ""), ""

    def _route(self, a: tuple, b: tuple) -> Route:
        key = "route:%.5f,%.5f>%.5f,%.5f" % (a + b)
        hit = self.store.cache_get(key)
        if hit:
            return Route(hit["km"], hit["min"])
        try:
            r = self.ors.route(a, b)
        except ApiError as exc:
            log.warning("Routing fehlgeschlagen, nutze Luftlinie: %s", exc)
            return straight_line_route(a, b)
        self.store.cache_put(key, {"km": r.distance_km, "min": r.duration_min})
        return r

    # ------------------------------------------------------------ Run
    def run(self, force_dry: bool = False) -> dict:
        with self._lock:
            dry = force_dry or self.cfg.dry_run
            now = self.now_fn()
            self._ors_down = False
            res: dict = {"time": _iso(now), "dry_run": dry, "ok": False, "error": None, "items": [],
                         "desired": None, "action": None, "notices": [], "vehicle": None, "effective": None,
                         "problems": [], "stale_since": None}
            try:
                self._run(res, now, dry)
            except EvccDown as exc:
                self._fail(res, exc, dry, Notice("evcc_down", "ALARM: evcc ist nicht erreichbar. %s Es wird nichts "
                                                 "am Ladeplan geändert." % exc, ttl_hours=6, level="error"))
            except ApiError as exc:
                self._fail(res, exc, dry, Notice("outage", "Ladeplanung gestört: %s" % exc, ttl_hours=12, level="error"))
            except Exception as exc:                               # never terminate the service
                res["error"] = "Interner Fehler: %s" % exc
                res["problems"].append(res["error"])
                log.exception("Interner Fehler")
            if res["ok"]:
                self._last_ok = res
            elif self._last_ok and not res["items"]:               # also show the last good state
                for k in ("items", "desired", "vehicle", "effective"):
                    res[k] = self._last_ok.get(k)
                res["stale_since"] = self._last_ok["time"]
            if not force_dry:
                self.store.put("last_run", res)
            self._state = res
            return res

    def _fail(self, res: dict, exc: Exception, dry: bool, notice: Notice) -> None:
        res["error"] = str(exc)
        res["problems"].append(str(exc))
        log.error("Lauf abgebrochen: %s", exc)
        self._send(res, [notice], dry)

    def busy(self) -> bool:
        return self._lock.locked()

    def last(self) -> dict:
        return self._state or self.store.get("last_run", {})

    def _run(self, res: dict, now: datetime, dry: bool) -> None:
        cfg = self.cfg
        state = self._retry(self.evcc.state, "evcc", EvccDown)
        vehicles = state.get("vehicles") or {}
        name = cfg.evcc_vehicle or (next(iter(vehicles)) if len(vehicles) == 1 else "")
        if name not in vehicles:
            raise ApiError("Fahrzeug '%s' nicht in evcc gefunden (vorhanden: %s)" % (name, ", ".join(vehicles) or "-"))
        veh = vehicles[name]
        capacity = veh.get("capacity")
        if not capacity:
            raise ApiError("evcc liefert keine Batteriekapazität für '%s'" % name)
        min_soc = float(veh.get("minSoc") or 0)
        lp = next((l for l in state.get("loadpoints") or [] if l.get("vehicleName") == name), {})
        res["vehicle"] = {"name": name, "title": veh.get("title") or name, "capacity": capacity,
                          "min_soc": min_soc, "soc": lp.get("vehicleSoc"), "car_limit": lp.get("vehicleLimitSoc"),
                          "connected": lp.get("connected"), "mode": lp.get("mode"),
                          "plan": veh.get("plan"), "repeating": veh.get("repeatingPlans") or []}

        events, cal_notices = self._events(res, now)
        fallback = self._fallback_temp(state)

        def temp_at(dt):
            t = forecast_temperature(state, dt)
            return t if t is not None else fallback

        self.store.prune_overrides(_iso(now - timedelta(days=2)))
        ev = planner.evaluate(events, now, cfg, float(capacity), min_soc, self.resolve, temp_at,
                              self.store.overrides())
        notices = list(ev.notices) + cal_notices
        if self._ors_down:
            res["problems"].append("OpenRouteService nicht erreichbar: Termine ohne gespeicherte Strecke werden nicht geplant")
            notices.append(Notice("ors_down", "OpenRouteService ist nicht erreichbar. Termine ohne gespeicherte "
                                  "Strecke werden nicht geplant.", ttl_hours=12))
        if ev.desired:
            notices += planner.context_notices(ev.desired, veh.get("repeatingPlans"), lp.get("vehicleLimitSoc"),
                                               cfg, float(capacity), now)
        current = veh.get("plan") or None
        last_set = self.store.get("last_set")
        action = planner.reconcile(ev.desired, current, last_set, now)
        res["items"] = [_item_dict(i) for i in ev.items]
        res["desired"] = None if not ev.desired else {
            "title": ev.desired.item.event.title, "soc": ev.desired.soc, "time": _iso(ev.desired.time),
            "capped_from": ev.desired.capped_from, "over100": ev.desired.over100}
        res["skipped_reason"] = ev.skipped_reason
        act = {"kind": action.kind, "reason": action.reason, "done": False, "error": None}
        res["action"] = act

        if action.kind == "manual":
            ct = current.get("time")
            notices.append(Notice("manual:%s:%s" % (current.get("soc"), ct),
                                  "In evcc steht ein manuell gesetzter Plan (%s %% bis %s). Der Dienst ändert ihn nicht. "
                                  "Gewünscht wäre %d %% bis %s für '%s'." % (
                                      current.get("soc"), ct, ev.desired.soc, _iso(ev.desired.time),
                                      ev.desired.item.event.title)))
        if action.kind in ("set", "delete") and not dry:
            try:
                if action.kind == "set":
                    self.evcc.set_plan(name, ev.desired.soc, ev.desired.time)
                    self.store.put("last_set", {"soc": ev.desired.soc, "time": _iso(ev.desired.time)})
                else:
                    self.evcc.delete_plan(name)
                    self.store.delete("last_set")
                act["done"] = True
                log.info("Aktion %s ausgeführt (%s)", action.kind, action.reason)
                if action.kind == "set":
                    notices.append(Notice("set:%d:%s" % (ev.desired.soc, _iso(ev.desired.time)),
                                          "Ladeplan gesetzt: %d %% bis %s für '%s'." % (
                                              ev.desired.soc, planner._fmt(ev.desired.time, cfg.timezone),
                                              ev.desired.item.event.title), level="info"))
                else:
                    notices.append(Notice("del:%s" % (last_set or {}).get("time"),
                                          "Ladeplan entfernt: Der zugehörige Termin existiert nicht mehr.", level="info"))
            except ApiError as exc:
                act["error"] = str(exc)
                notices.append(Notice("evccwrite", "Plan konnte nicht in evcc gesetzt werden: %s" % exc, ttl_hours=6, level="error"))
        elif action.kind in ("set", "delete"):
            log.info("Dry-Run: Aktion %s (%s) nicht ausgeführt", action.kind, action.reason)

        self._check_effective(res, lp, last_set if action.kind == "none" else None, now, notices)
        self._send(res, notices, dry)
        res["ok"] = True
        self.store.prune()

    def _events(self, res: dict, now: datetime) -> tuple:
        """Read the calendar; on failure continue with the last state read."""
        end = now + timedelta(days=self.cfg.horizon_days)
        try:
            events = self._retry(lambda: self.ha.calendar_events(self.cfg.ha_calendar, now, end), "Home Assistant (Kalender)")
        except ApiError as exc:
            cache = self.store.get("events_cache")
            if not cache:
                raise
            events = [Event(uid=d["uid"], title=d["title"], start=datetime.fromisoformat(d["start"]),
                            end=datetime.fromisoformat(d["end"]), location=d["location"], description=d["description"])
                      for d in cache["events"]]
            events = [e for e in events if now <= e.start <= end]
            res["stale_since"] = cache["time"]
            msg = ("Kalender nicht lesbar, Planung nach Stand vom %s (%s)"
                   % (cache["time"], exc))
            res["problems"].append(msg)
            log.error(msg)
            return events, [Notice("calendar_down", msg, ttl_hours=6, level="error")]
        self.store.put("events_cache", {"time": _iso(now), "events": [
            {"uid": e.uid, "title": e.title, "start": e.start.isoformat(), "end": e.end.isoformat(),
             "location": e.location, "description": e.description} for e in events]})
        return events, []

    def _fallback_temp(self, state: dict) -> Optional[float]:
        t = state.get("tariffTemperature")
        if isinstance(t, (int, float)):
            return float(t)
        try:
            return self.ha.weather_temperature(self.cfg.ha_weather)
        except ApiError:
            return None

    def _check_effective(self, res, lp, last_set, now, notices) -> None:
        eid = lp.get("effectivePlanId")
        res["effective"] = {"id": eid, "soc": lp.get("effectivePlanSoc"), "time": lp.get("effectivePlanTime")}
        if not last_set:
            return
        t = planner._parse(last_set["time"])
        if t and eid == 0 and t - now < timedelta(days=2):
            notices.append(Notice("noteffective:%s" % last_set["time"],
                                  "Der gesetzte Plan (%s %% bis %s) ist in evcc nicht wirksam." % (
                                      last_set["soc"], last_set["time"]), ttl_hours=12))

    def _send(self, res: dict, notices: list, dry: bool) -> None:
        seen = set()
        for n in notices:
            if n.key in seen:
                continue
            seen.add(n.key)
            entry = {"text": n.text, "status": "", "level": n.level}
            if self.store.was_notified(n.key, n.ttl_hours):
                entry["status"] = "bereits gemeldet"
            elif dry:
                entry["status"] = "Dry-Run: würde senden"
            elif not self.cfg.notify_targets.get(n.level):
                entry["status"] = "kein Ziel für Stufe %s konfiguriert" % n.level
            else:
                try:
                    title = {"info": "Ladeplanung", "warning": "Ladeplanung: Warnung", "error": "Ladeplanung: FEHLER"}[n.level]
                    self.ha.notify(self.cfg.notify_targets[n.level], title, n.text)
                    self.store.mark_notified(n.key)
                    entry["status"] = "gesendet"
                except ApiError as exc:
                    entry["status"] = "Senden fehlgeschlagen: %s" % exc
                    msg = "Push über Home Assistant nicht möglich: %s" % exc
                    if msg not in res["problems"]:
                        res["problems"].append(msg)
                    log.error("%s | Hinweis: %s", msg, n.text)
            res["notices"].append(entry)


def _item_dict(i) -> dict:
    return {
        "key": override_key(i.event), "has_location": bool((i.event.location or "").strip()),
        "override_mode": i.override.mode if i.override else None,
        "override_target": i.override.target if i.override else None, "manual": i.manual,
        "title": i.event.title, "start": _iso(i.event.start), "end": _iso(i.event.end),
        "location": i.event.location, "status": i.status, "detail": i.detail,
        "mode": i.cls.mode.value if i.cls else None, "unclear": bool(i.cls and i.cls.unclear),
        "place": i.place_label,
        "km": None if not i.route else round(i.route.distance_km, 1),
        "drive_min": None if not i.route else round(i.route.duration_min),
        "estimated": bool(i.route and i.route.estimated),
        "temp_c": i.temp_c, "kwh100": i.kwh100,
        "need_soc": None if i.need_soc is None else round(i.need_soc, 1),
        "ready_by": _iso(i.ready_by), "target_alone": i.target_alone, "target_chain": i.target_chain,
    }
