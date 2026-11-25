"""One run: read calendar, plan, reconcile with evcc, send notices."""
from __future__ import annotations

import dataclasses
import logging
import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional

from . import address, calc, chargeview, planner
from .clients import (ApiError, EvccClient, EvccDown, HAClient, IcsClient, OrsClient, OrsDown,
                      forecast_temperature, straight_line_route)
from .models import Event
from .config import Config
from .i18n import t
from .models import Notice, Route, override_key
from .notify import Notifier
from .settings import Settings
from .store import Store

log = logging.getLogger("evccplan")
GOOD_LAYERS = ("address", "venue")


PUSH_RETRY_S = 300          # after a failed push `watch` waits this long before trying the same notice again
UNPLUGGED_LEAD_MIN = 10     # warn this long before the planned charge start if the car is not plugged in
LIMIT_RETRY_H = 3.0         # car charge limit still too low this long after setting it: try again
LIMIT_MAX_TRIES = 2         # then give up and tell the user
EARLIER_MIN = 10            # tell when charging has to start at least this much earlier than assumed


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


class Runner:
    def __init__(self, cfg: Config, store: Store, ha: HAClient, evcc: EvccClient, ors: Optional[OrsClient],
                 now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.cfg, self.store, self.ha, self.evcc, self.ors, self.now_fn = cfg, store, ha, evcc, ors, now_fn
        self.settings = Settings(cfg, store)
        self.notifier = Notifier(cfg, store)
        self._lock = threading.Lock()
        self._state: dict = {}
        self._last_ok: dict = {}
        self._sessions_after = 0.0                              # monotonic time before which sessions are not read again
        self._ors_down = False
        self._watch_retry: dict = {}                            # notice key -> monotonic time of the next try after a failed push
        self.mqtt = None                                        # MqttClient, set by the service (publishes the plan)
        self._cal_names: dict = {}                              # calendar entity -> display name
        self.ics = {n: IcsClient(n, u, cfg.timezone) for n, u in cfg.ics_calendars}

    def _t(self, key: str, **kw) -> str:
        return t(self.cfg.language, "runner." + key, **kw)

    # ------------------------------------------------------------ Addresses and routes
    def _retry(self, fn, what: str, down_cls=ApiError):
        """Up to `retries` attempts with growing pauses; then down_cls."""
        last = None
        for n in range(max(1, self.cfg.retries)):
            try:
                return fn()
            except ApiError as exc:
                last = exc
                log.warning("%s: attempt %d/%d failed (%s)", what, n + 1, self.cfg.retries, exc)
                if n < self.cfg.retries - 1 and self.cfg.retry_delay_s > 0:
                    time.sleep(self.cfg.retry_delay_s * (3 ** n))
        raise down_cls(self._t("api_no_response", what=what, n=self.cfg.retries, detail=last))

    def _ors_call(self, fn):
        if self._ors_down:
            raise OrsDown(self._t("ors_previously_down"))
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
            raise ApiError(self._t("home_missing"))
        p = self._ors_call(lambda: self.ors.geocode(self.cfg.home_address))
        if not p:
            raise ApiError(self._t("home_not_found"))
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
            return None, self._t("ors_key_missing"), ""
        best_note = ""
        try:
            home = self._home()
            for q in address.candidates(event.location):
                g = self._geocode(q)
                if not g:
                    continue
                if g["layer"] in GOOD_LAYERS and g["confidence"] >= self.cfg.ors_min_confidence:
                    dest = (g["lat"], g["lon"])
                    return dataclasses.replace(self._route(home, dest), dest=dest), None, g["label"]
                best_note = best_note or self._t("best_hit", label=g["label"], layer=g["layer"],
                                                 conf="%.1f" % g["confidence"])
        except OrsDown as exc:
            return None, self._t("ors_unreachable", detail=exc), ""
        if best_note:
            return None, self._t("address_unclear_note", note=best_note), ""
        return None, self._t("address_unclear"), ""

    def _route(self, a: tuple, b: tuple) -> Route:
        key = "route:%.5f,%.5f>%.5f,%.5f" % (a + b)
        hit = self.store.cache_get(key)
        if hit:
            return Route(hit["km"], hit["min"])
        try:
            r = self.ors.route(a, b)
        except ApiError as exc:
            log.warning("Routing failed, using straight-line distance: %s", exc)
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
                         "problems": [], "stale_since": None, "limit_fix": None}
            try:
                self._run(res, now, dry)
            except EvccDown as exc:
                self._fail(res, exc, dry, Notice("evcc_down", self._t("evcc_down", detail=exc), ttl_hours=6, level="error"))
            except ApiError as exc:
                self._fail(res, exc, dry, Notice("outage", self._t("outage", detail=exc), ttl_hours=12, level="error"))
            except Exception as exc:                               # never terminate the service
                res["error"] = self._t("internal_error", detail=exc)
                res["problems"].append(res["error"])
                log.exception("Internal error")
            if res["ok"]:
                self._last_ok = res
            elif self._last_ok and not res["items"]:               # also show the last good state
                for k in ("items", "desired", "vehicle", "effective"):
                    res[k] = self._last_ok.get(k)
                res["stale_since"] = self._last_ok["time"]
            if not force_dry:
                self.store.put("last_run", res)
            self._state = res
            self._record(res)
            if self.mqtt is not None:
                self.mqtt.publish_run(res)
            return res

    def _record(self, res: dict) -> None:
        """One compact history entry per run (shown in the UI)."""
        act, dz = res.get("action") or {}, res.get("desired") or {}
        problems = [str(x)[:300] for x in (res.get("problems") or [])[:5]]
        noteworthy = (not res["ok"] or problems or act.get("error")
                      or act.get("kind") in ("set", "delete"))
        if not noteworthy:                                     # "no change" runs are not listed, the UI shows the last run time
            return
        try:
            rec = {
                "time": res["time"], "dry_run": res["dry_run"], "ok": res["ok"], "error": res["error"],
                "action": act.get("kind"), "reason": act.get("reason"), "done": act.get("done"),
                "action_error": act.get("error"), "soc": dz.get("soc"), "plan_time": dz.get("time"),
                "title": dz.get("title"), "items": len(res.get("items") or []),
                "notices": len(res.get("notices") or []), "problems": len(res.get("problems") or []),
                "problem_texts": problems,
            }
            sig = [rec[k] for k in ("dry_run", "ok", "error", "action", "done", "action_error", "soc", "plan_time", "problem_texts")]
            last = (self.store.runs(1) or [None])[0]
            if last and last.get("sig") == sig:                # same outcome as the previous entry: count it, do not repeat it
                last["count"] = last.get("count", 1) + 1
                last["last_time"] = rec["time"]
                self.store.run_update_last(last)
            else:
                rec["sig"] = sig
                self.store.run_add(rec)
        except Exception:                                      # history must never break a run
            log.exception("Could not save run history")

    def _fail(self, res: dict, exc: Exception, dry: bool, notice: Notice) -> None:
        res["error"] = str(exc)
        res["problems"].append(str(exc))
        log.error("Run aborted: %s", exc)
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
            raise ApiError(self._t("vehicle_not_found", name=name, available=", ".join(vehicles) or "-"))
        veh = vehicles[name]
        capacity = veh.get("capacity")
        if not capacity:
            raise ApiError(self._t("no_capacity", name=name))
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
                              self.store.overrides(), leg=self._route)
        notices = list(ev.notices) + cal_notices
        if self._ors_down:
            res["problems"].append(self._t("ors_down_problem"))
            notices.append(Notice("ors_down", self._t("ors_down_notice"), ttl_hours=12))
        if ev.desired:
            notices += planner.context_notices(ev.desired, veh.get("repeatingPlans"), lp.get("vehicleLimitSoc"),
                                               cfg, float(capacity), now)
        notices += self._limit_fix(res, lp, ev.desired, now, dry)
        res["vehicle"]["limit_fix"] = res.get("limit_fix")
        fx = res.get("limit_fix") or {}
        suffix = {"pending": lambda: self._t("limit_trying", value=cfg.limit_value),
                  "dry": lambda: self._t("limit_dry", value=cfg.limit_value),
                  "failed": lambda: self._t("limit_failed", detail=fx.get("error"))}.get(fx.get("state"))
        if suffix:                                            # tell in the "limit too low" warning that a correction is under way
            for n in notices:
                if n.key.startswith("carlimit:"):
                    n.text += " " + suffix()
        current = veh.get("plan") or None
        last_set = self.store.get("last_set")
        action = planner.reconcile(ev.desired, current, last_set, now, cfg.language)
        ests = self._estimates(ev, state, lp, float(capacity))
        res["items"] = [_item_dict(i, ests.get(id(i))) for i in ev.items]
        res["desired"] = None if not ev.desired else {
            "title": ev.desired.item.event.title, "soc": ev.desired.soc, "time": _iso(ev.desired.time),
            "capped_from": ev.desired.capped_from, "over100": ev.desired.over100}
        res["skipped_reason"] = ev.skipped_reason
        act = {"kind": action.kind, "reason": action.reason, "done": False, "error": None}
        res["action"] = act

        if action.kind == "manual":
            ct = current.get("time")
            notices.append(Notice("manual:%s:%s" % (current.get("soc"), ct),
                                  self._t("manual_plan", soc=current.get("soc"), time=ct,
                                          want_soc="%d" % ev.desired.soc, want_time=_iso(ev.desired.time),
                                          title=ev.desired.item.event.title)))
        if action.kind in ("set", "delete") and not dry:
            try:
                if action.kind == "set":
                    self.evcc.set_plan(name, ev.desired.soc, ev.desired.time)
                    self.store.put("last_set", {"soc": ev.desired.soc, "time": _iso(ev.desired.time),
                                                "slack_min": ev.desired.slack_min})
                else:
                    self.evcc.delete_plan(name)
                    self.store.delete("last_set")
                act["done"] = True
                log.info("Action %s executed (%s)", action.kind, action.reason)
                if action.kind == "set":
                    text = self._t("plan_set", soc="%d" % ev.desired.soc, time=planner._fmt(ev.desired.time, cfg.timezone),
                                  title=ev.desired.item.event.title)
                    soc_now = lp.get("vehicleSoc")
                    if isinstance(soc_now, (int, float)) and soc_now >= ev.desired.soc:
                        text += " " + self._t("plan_set_no_charge_needed", soc="%d" % round(soc_now))
                    else:
                        text += self._cost_text(state, lp, ev.desired.soc, float(capacity))
                    notices.append(Notice("set:%d:%s" % (ev.desired.soc, _iso(ev.desired.time)), text, level="info"))
                else:
                    notices.append(Notice("del:%s" % (last_set or {}).get("time"),
                                          self._t("plan_removed"), level="info"))
            except ApiError as exc:
                act["error"] = str(exc)
                notices.append(Notice("evccwrite", self._t("evcc_write_failed", detail=exc), ttl_hours=6, level="error"))
        elif action.kind in ("set", "delete"):
            log.info("Dry run: action %s (%s) not executed", action.kind, action.reason)

        self._check_effective(res, lp, last_set if action.kind == "none" else None, now, notices)
        self._check_overrun(lp, notices)
        self._sessions(state, name, veh.get("title") or name, now)
        self._send(res, notices, dry)
        res["ok"] = True
        self.store.prune()

    def watch(self, snap: dict) -> None:
        """Pushes only (no planning run): car not plugged in shortly before the charge start, start earlier than
        assumed, plan no longer reachable. Works on the live poller's cached view; skipped while a run is busy."""
        view = (snap or {}).get("charge")
        if not view or view.get("state") == "done" or not self._lock.acquire(blocking=False):
            return
        try:
            now = self.now_fn()
            deadline = planner._parse(view.get("deadline"))
            w_start, w_end = planner._parse(view["window"]["start"]), planner._parse(view["window"]["end"])
            if not (deadline and w_start and w_end and now < deadline):
                return
            tz, target = self.cfg.timezone, view["target"]
            when = lambda dt: planner._fmt(dt, tz)
            notices = []
            if view["state"] == "away":
                if now >= w_start - timedelta(minutes=UNPLUGGED_LEAD_MIN):
                    notices.append(Notice("unplugged:%s" % view["deadline"], self._t(
                        "unplugged", soc="%d" % round(target), time=when(deadline), start=when(w_start))))
            elif view.get("soc") is not None:
                soc = view["soc"]
                expected = deadline - timedelta(hours=calc.hours_to_charge(
                    target - self.cfg.reserve_soc, float(view["capacity"]), self.cfg.charge_power_kw, self.cfg.charge_loss_margin))
                # soc check: with a dynamic tariff evcc may start early for cheap slots although the car came back full
                if soc < self.cfg.reserve_soc and expected - w_start >= timedelta(minutes=EARLIER_MIN):
                    notices.append(Notice("earlier:%s" % view["deadline"], self._t(
                        "start_earlier", soc="%d" % round(soc), reserve="%d" % round(self.cfg.reserve_soc),
                        target="%d" % round(target), start=when(w_start), expected=when(expected)), level="info"))
                left_h = calc.hours_to_charge(target - soc, float(view["capacity"]), self.cfg.charge_power_kw, self.cfg.charge_loss_margin)
                end = w_end if not view["window"]["estimated"] else max(now, w_start) + timedelta(hours=left_h)
                # the buffer is planned into the deadline: only what exceeds plan time + buffer is really late
                late_min = (end - deadline).total_seconds() / 60 - self._slack_min(deadline)
                if late_min >= max(1.0, self.cfg.notify_min_late_min):
                    notices.append(Notice("overrun:%s" % view["deadline"], self._t(
                        "plan_overrun", soc="%d" % round(target), time=when(deadline),
                        late=self._t("plan_overrun_by", min="%d" % round(late_min))), ttl_hours=12))
            mono = time.monotonic()
            notices = [n for n in notices if self._watch_retry.get(n.key, 0.0) <= mono]
            if notices:
                out = {"notices": [], "problems": []}
                self._send(out, notices, self.cfg.dry_run)
                if out["problems"]:                                # push failed: do not retry every minute
                    for n in notices:
                        self._watch_retry[n.key] = mono + PUSH_RETRY_S
        except Exception:                                      # the watcher must never break the service
            log.exception("Watch failed")
        finally:
            self._lock.release()

    def _events(self, res: dict, now: datetime) -> tuple:
        """Read all calendars. A failing one falls back to its last state read; the others are planned regardless."""
        end = now + timedelta(days=self.cfg.horizon_days)
        events, notices, served, stale, last_exc = [], [], 0, [], None
        if self.cfg.ha_calendars:
            try:
                self._cal_names = self.ha.calendar_names()
            except (ApiError, KeyError, TypeError, AttributeError):     # label only: keep the last names, else the entity id
                pass
        # (cache/notice id, label, reader): Home Assistant calendars by entity, ICS feeds by their configured name
        sources = [(cal, self._cal_names.get(cal, cal), lambda cal=cal: self.ha.calendar_events(cal, now, end),
                    "Home Assistant (calendar %s)" % cal) for cal in self.cfg.ha_calendars]
        sources += [("ics:" + n, n, lambda c=c: c.events(now, end), "ICS calendar %s" % n) for n, c in self.ics.items()]
        for cal, label, read, what in sources:
            key = "events_cache:" + cal
            try:
                got = self._retry(read, what)
            except ApiError as exc:
                cache = self.store.get(key)
                last_exc = exc
                if not cache:
                    msg = self._t("calendar_down_nocache", calendar=label, detail=exc)
                else:
                    got = [Event(uid=d["uid"], title=d["title"], start=datetime.fromisoformat(d["start"]),
                                 end=datetime.fromisoformat(d["end"]), location=d["location"], description=d["description"])
                           for d in cache["events"]]
                    events += [dataclasses.replace(e, calendar=label) for e in got if now <= e.start <= end]
                    served += 1
                    stale.append(cache["time"])
                    msg = self._t("calendar_down", calendar=label, time=cache["time"], detail=exc)
                res["problems"].append(msg)
                log.error(msg)
                notices.append(Notice("calendar_down:" + cal, msg, ttl_hours=6, level="error"))
                continue
            self.store.put(key, {"time": _iso(now), "events": [
                {"uid": e.uid, "title": e.title, "start": e.start.isoformat(), "end": e.end.isoformat(),
                 "location": e.location, "description": e.description} for e in got]})
            served += 1
            events += [dataclasses.replace(e, calendar=label) for e in got]
        if last_exc and not served:                                     # nothing readable at all: abort the run
            raise last_exc
        if stale:
            res["stale_since"] = min(stale)
        # a shared appointment can show up in several calendars with the same UID; keep the first
        seen, unique = set(), []
        for e in events:
            if (e.uid, e.start) not in seen:
                seen.add((e.uid, e.start))
                unique.append(e)
        return unique, notices

    def _fallback_temp(self, state: dict) -> Optional[float]:
        t = state.get("tariffTemperature")
        if isinstance(t, (int, float)):
            return float(t)
        try:
            return self.ha.weather_temperature(self.cfg.ha_weather)
        except ApiError:
            return None

    def _limit_fix(self, res, lp, desired, now, dry) -> list:
        """Car charge limit below what the plan needs: raise it once via Home Assistant, confirm via evcc's vehicleLimitSoc."""
        cfg, out = self.cfg, []
        limit = lp.get("vehicleLimitSoc")
        st = self.store.get("limitfix")                       # {"time","from","plan","tries"}
        low = isinstance(limit, (int, float)) and not isinstance(limit, bool) and 0 < limit < cfg.vehicle_limit_min
        if not low:
            if st and isinstance(limit, (int, float)) and limit > 0:
                out.append(Notice("carlimit_ok:%s" % st["time"], self._t("limit_ok", limit="%d" % limit), level="info"))
                self.store.delete("limitfix")
            return out
        if not (cfg.limit_entity and cfg.limit_auto and desired):
            return out
        plan = _iso(desired.time)
        if st and st.get("plan") != plan:
            st = None                                         # new plan: start over
        last = planner._parse(st["time"]) if st else None
        due = st is None or (now - last >= timedelta(hours=LIMIT_RETRY_H) and st["tries"] < LIMIT_MAX_TRIES)
        if due:
            if st:                                            # previous attempt had no effect
                out.append(Notice("carlimit_retry:%s:%d" % (plan, st["tries"]), self._t(
                    "limit_retry", limit="%d" % limit, hours="%g" % LIMIT_RETRY_H, value=cfg.limit_value)))
            if dry:
                res["limit_fix"] = {"state": "dry"}
            else:
                try:
                    self.ha.set_number(cfg.limit_entity, cfg.limit_value)
                    st = {"time": _iso(now), "from": limit, "plan": plan, "tries": (st or {}).get("tries", 0) + 1}
                    self.store.put("limitfix", st)
                except ApiError as exc:
                    res["limit_fix"] = {"state": "failed", "error": str(exc)}
        elif st and st["tries"] >= LIMIT_MAX_TRIES and now - last >= timedelta(hours=LIMIT_RETRY_H):
            out.append(Notice("carlimit_retry:%s:gaveup" % plan, self._t("limit_gave_up", limit="%d" % limit)))
        if st and res.get("limit_fix") is None:
            res["limit_fix"] = {"state": "pending", "since": st["time"], "value": cfg.limit_value}
        return out

    def _check_effective(self, res, lp, last_set, now, notices) -> None:
        eid = lp.get("effectivePlanId")
        res["effective"] = {"id": eid, "soc": lp.get("effectivePlanSoc"), "time": lp.get("effectivePlanTime")}
        if not last_set:
            return
        t = planner._parse(last_set["time"])
        soc = lp.get("vehicleSoc")
        if isinstance(soc, (int, float)) and soc >= last_set["soc"]:
            return                          # battery is already at or above the target: evcc has nothing to charge, not a fault
        if t and eid == 0 and timedelta(0) < t - now < timedelta(days=2):   # past plans are history: no push
            notices.append(Notice("noteffective:%s" % last_set["time"],
                                  self._t("not_effective", soc=last_set["soc"], time=planner._fmt(t, self.cfg.timezone)), ttl_hours=12))

    def _estimates(self, ev, state: dict, lp: dict, capacity: float) -> dict:
        """Window and cost estimate per planned, chained or later calendar item (id(item) -> view); never raises."""
        out: dict = {}
        try:
            chain = [i for i in ev.items if i.status in ("geplant", "verkettet")]
            known = lp.get("connected") is True and isinstance(lp.get("vehicleSoc"), (int, float))
            for k, it in enumerate(chain):
                if it.leg_from:
                    continue                       # follower of a tour: the car does not come home, nothing to charge
                target = float(ev.desired.soc) if k == 0 and ev.desired else float(it.target_chain)
                if k == 0:
                    start, earliest = (float(lp["vehicleSoc"]) if known else float(self.cfg.reserve_soc)), None
                    guess = not known
                else:
                    prev = chain[k - 1]            # last appointment of the previous tour: the car is back home after it
                    start, earliest, guess = prev.home_soc, prev.back_home, True
                out[id(it)] = chargeview.estimate_view(target, start, it.ready_by, capacity, self.cfg, state, earliest, guess)
            # later appointments (outside the chain window) are not planned yet: estimate each on its own, car back at the reserve
            for it in ev.items:
                if it.status == "spaeter" and it.target_alone is not None and it.ready_by:
                    out[id(it)] = chargeview.estimate_view(float(it.target_alone), float(self.cfg.reserve_soc), it.ready_by,
                                                           capacity, self.cfg, state)
        except Exception:                                      # an estimate must never break a run
            log.exception("Charge estimate failed")
        return out

    def _money(self, value: float, currency: str) -> str:
        text = "%.2f" % value
        if self.cfg.language == "de":
            text = text.replace(".", ",")
        return text + (" €" if currency == "EUR" else " " + currency)

    def _cost_text(self, state: dict, lp: dict, target: float, capacity: float) -> str:
        """Sentence with the maximum cost for the push message; empty when evcc has no price."""
        price, currency, _ = chargeview.price_of(state)
        known = lp.get("connected") is True and isinstance(lp.get("vehicleSoc"), (int, float))
        start = float(lp["vehicleSoc"]) if known else float(self.cfg.reserve_soc)
        cost = chargeview.max_cost(target, start, capacity, self.cfg.charge_loss_margin, price)
        if not cost:
            return ""
        return " " + self._t("plan_set_cost" if known else "plan_set_cost_est", cost=self._money(cost["max"], currency),
                             kwh="%.1f" % cost["kwh"])

    def _check_overrun(self, lp: dict, notices: list) -> None:
        """evcc reports a plan that cannot be reached in time (planOverrun); tell once per plan time.

        The charge buffer is already planned into the plan time, so evcc finishing a little after it is expected:
        only a finish later than plan time + buffer (and at least `notify_min_late_min` of it) is reported."""
        over = lp.get("planOverrun")
        plan_time, end = planner._parse(lp.get("effectivePlanTime")), planner._parse(lp.get("planProjectedEnd"))
        if isinstance(over, bool) or not isinstance(over, (int, float)) or over <= 0 or not plan_time or plan_time <= self.now_fn():
            return                                              # a past plan is history: no push
        if end is None:
            end = plan_time + timedelta(seconds=over / 1e9)     # evcc reports planOverrun as a duration in nanoseconds
        late_min = (end - plan_time).total_seconds() / 60 - self._slack_min(plan_time)
        if late_min < max(1.0, self.cfg.notify_min_late_min):
            return
        notices.append(Notice("overrun:%s" % _iso(plan_time),
                              self._t("plan_overrun", soc="%d" % round(lp.get("effectivePlanSoc") or 0),
                                      time=planner._fmt(plan_time, self.cfg.timezone),
                                      late=self._t("plan_overrun_by", min="%d" % round(late_min))),
                              ttl_hours=12))

    def _slack_min(self, plan_time) -> float:
        """Buffer that was planned in before `plan_time` (the real latest time is plan time + slack)."""
        ls = self.store.get("last_set") or {}
        t = planner._parse(ls.get("time"))
        if t and plan_time and abs((t - plan_time).total_seconds()) < 60 and isinstance(ls.get("slack_min"), (int, float)):
            return float(ls["slack_min"])
        return float(self.cfg.charge_buffer_min)                # plan set by hand in evcc, or before 0.7.0

    def _sessions(self, state: dict, name: str, title: str, now: datetime) -> None:
        """Finished charging sessions of the vehicle with their real cost go into the history (once each)."""
        if time.monotonic() < self._sessions_after:               # at most every 30 min, 6 h after a failure
            return
        self._sessions_after = time.monotonic() + 1800
        try:
            months = {(now.year, now.month), ((now - timedelta(days=3)).year, (now - timedelta(days=3)).month)}
            sessions = [s for ym in sorted(months) for s in self.evcc.sessions(*ym)]
        except ApiError as exc:
            self._sessions_after = time.monotonic() + 6 * 3600
            log.debug("Charging sessions not readable: %s", exc)        # e.g. evcc database off; not worth a warning per run
            return
        for s in sorted((x for x in sessions if isinstance(x, dict)), key=lambda x: str(x.get("finished"))):
            fin, sid = planner._parse(s.get("finished")), s.get("id")
            price, kwh = s.get("price"), s.get("chargedEnergy")
            if (fin is None or fin.year < 2000 or fin < now - timedelta(days=3) or not isinstance(sid, int)
                    or s.get("vehicle") not in (name, title) or not isinstance(price, (int, float))
                    or not isinstance(kwh, (int, float)) or self.store.was_notified("session:%d" % sid, None)):
                continue
            solar = s.get("solarPercentage")
            self.store.run_add({"kind": "session", "time": _iso(fin), "ok": True, "error": None, "problems": 0,
                                "kwh": round(kwh, 1), "price": round(price, 2),
                                "currency": state.get("currency") or chargeview.DEFAULT_CURRENCY,
                                "solar": round(solar) if isinstance(solar, (int, float)) else None,
                                "soc_start": s.get("socStart"), "soc_end": s.get("socEnd")})
            self.store.mark_notified("session:%d" % sid)

    def _send(self, res: dict, notices: list, dry: bool) -> None:
        quiet = self.notifier.in_quiet(self.now_fn())
        if not quiet and not dry:                           # quiet hours over: deliver what was held back first
            held = [Notice(h["key"], h["text"], h.get("ttl"), h.get("level") or "warning") for h in self.notifier.take_held()]
            notices = held + list(notices)
        seen = set()
        for n in notices:
            if n.key in seen:
                continue
            seen.add(n.key)
            entry = {"text": n.text, "status": "", "level": n.level}
            if self.store.was_notified(n.key, n.ttl_hours):
                entry["status"] = self._t("status_already")
            elif dry:
                entry["status"] = self._t("status_dry")
            elif not (targets := self.notifier.targets(n)):
                entry["status"] = self._t("status_no_target", level=n.level)
            elif quiet and n.level != "error":
                self.notifier.hold(n)
                entry["status"] = self._t("status_held")
            else:
                try:
                    title = self._t("push_title_" + n.level)
                    self.ha.notify(targets, title, n.text)
                    self.store.mark_notified(n.key)
                    entry["status"] = self._t("status_sent")
                except ApiError as exc:
                    entry["status"] = self._t("status_failed", detail=exc)
                    msg = self._t("push_failed", detail=exc)
                    if msg not in res["problems"]:
                        res["problems"].append(msg)
                    log.error("%s | notice: %s", msg, n.text)
            res["notices"].append(entry)


def _r1(x):
    return None if x is None else round(x, 1)


def _item_dict(i, charge_est=None) -> dict:
    return {
        "charge_est": charge_est,
        "key": override_key(i.event), "has_location": bool((i.event.location or "").strip()),
        "override_mode": i.override.mode if i.override else None,
        "override_target": i.override.target if i.override else None, "manual": i.manual,
        "title": i.event.title, "calendar": i.event.calendar, "start": _iso(i.event.start), "end": _iso(i.event.end),
        "location": i.event.location, "status": i.status, "detail": i.detail,
        "mode": i.cls.mode.value if i.cls else None, "unclear": bool(i.cls and i.cls.unclear),
        "place": i.place_label,
        "km": None if not i.route else round(i.route.distance_km, 1),
        "drive_min": None if not i.route else round(i.route.duration_min),
        "estimated": bool(i.route and i.route.estimated),
        "temp_c": i.temp_c, "kwh100": i.kwh100,
        "need_soc": None if i.need_soc is None else round(i.need_soc, 1),
        "ready_by": _iso(i.ready_by), "target_alone": i.target_alone,
        "target_chain": None if i.target_chain is None else round(i.target_chain, 1),
        "tour_size": i.tour_size, "tour_km": None if i.tour_km is None else round(i.tour_km, 1),
        "tour_out_soc": _r1(i.tour_out_soc), "tour_legs_soc": [_r1(x) for x in i.tour_legs_soc], "tour_back_soc": _r1(i.tour_back_soc),
        "leg_from": i.leg_from or None, "leg_in_soc": _r1(i.leg_in_soc), "leg_home_soc": _r1(i.leg_home_soc),
        "leg_in_km": None if not i.leg_in else round(i.leg_in.distance_km, 1),
        "leg_in_min": None if not i.leg_in else round(i.leg_in.duration_min),
        "arrival_soc": _r1(i.arrival_soc),
    }
