"""Pure planning logic, no network.

evaluate(): events -> desired plan (target SoC + time) and notices.
context_notices(): notices that need the evcc state.
reconcile(): desired plan versus the existing plan in evcc.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from . import calc, i18n
from .classify import classify
from .models import Desired, Evaluation, Item, Mode, Notice, Override, Route, override_key

Resolver = Callable[[object], tuple]          # Event -> (Route|None, error text|None, place name)
TempAt = Callable[[datetime], Optional[float]]


def floor5min(dt: datetime) -> datetime:
    dt = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return dt - timedelta(minutes=dt.minute % 5)


def _g(x) -> str:
    """Number formatted like %g."""
    return "%g" % x


def _translator(lang: str) -> Callable:
    return lambda key, **kw: i18n.t(lang, key, **kw)


def _fmt(dt: datetime, tz: str) -> str:
    return dt.astimezone(ZoneInfo(tz)).strftime("%a %d.%m. %H:%M")


def evaluate(events: list, now: datetime, cfg, capacity_kwh: float, min_soc: float,
             resolve: Resolver, temp_at: TempAt, overrides: Optional[dict] = None) -> Evaluation:
    """overrides: {override_key(event): Override} from the UI."""
    T = _translator(getattr(cfg, "language", "en"))
    ev = Evaluation()
    notices = ev.notices
    overrides = overrides or {}
    manual_drive = timedelta(minutes=cfg.manual_drive_min)
    # 1. classify each event, determine route and demand
    for e in sorted(events, key=lambda x: x.start):
        it = Item(event=e)
        ev.items.append(it)
        ov = overrides.get(override_key(e)) or Override()
        it.override = ov if (ov.mode or ov.target is not None) else None
        has_loc = bool((e.location or "").strip())
        if ov.mode == "none":
            it.status, it.detail = "manuell_aus", T("planner.manual_off")
            continue
        # Car is set if "Auto" was chosen or a target was given (a target means: charge here)
        forced = ov.mode == "car" or ov.target is not None
        if not has_loc and not forced:
            it.status, it.detail = "kein_ort", T("planner.no_location")
            continue
        if has_loc:
            it.cls = classify(e.title, e.description, cfg.rules)
            if it.cls.mode == Mode.BAHN and not forced:
                it.status, it.detail = "bahn", T("planner.cls." + it.cls.reason, rule=it.cls.reason_arg)
                continue
            route, err, label = resolve(e)
            it.place_label = label or ""
        else:
            route, err = None, T("planner.no_location_given")
        if route is None:
            if ov.target is None:
                if has_loc:
                    it.status, it.detail = "adresse_unklar", err or T("planner.address_unresolvable")
                    if not (err or "").startswith(T("runner.ors_unreachable", detail="").split("(")[0].strip()):     # outage is reported once, collectively
                        notices.append(Notice("addr:%s" % e.uid,
                                              T("planner.notice.address_unclear", title=e.title, detail=it.detail)))
                else:
                    it.status, it.detail = "ziel_fehlt", T("planner.car_no_location")
                    notices.append(Notice("notarget:%s" % e.uid,
                                          T("planner.notice.no_target", title=e.title)))
                continue
            # route unknown but target given: plan with an assumed travel time
            it.manual = True
            drive = manual_drive
            it.detail = T("planner.manual_target", min=_g(cfg.manual_drive_min))
            if has_loc:
                it.temp_c = temp_at(e.start)
        else:
            it.route = route
            if route.distance_km < cfg.min_car_km and not forced and not (it.cls and it.cls.explicit):
                it.status, it.detail = "nah", T("planner.too_near", km=_g(cfg.min_car_km))
                continue
            it.temp_c = temp_at(e.start)
            it.kwh100 = calc.kwh_per_100(it.temp_c, cfg.warm, cfg.cold, cfg.temp_threshold_c)
            it.need_soc = calc.round_trip_need_soc(route.distance_km, it.kwh100, capacity_kwh)
            it.target_alone, _ = _target(it.need_soc, cfg.reserve_soc)
            drive = timedelta(minutes=route.duration_min)
            if route.estimated:
                it.detail = T("planner.estimated_route")
        it.ready_by = floor5min(e.start - drive - timedelta(minutes=cfg.time_buffer_min))
        it.back_home = e.end + drive
        if it.ready_by <= now:
            it.status, it.detail = "zu_spaet", T("planner.too_late")
            continue
        it.status = "auto"

    # 2. chain of car events, exclude overlaps
    autos = [i for i in ev.items if i.status == "auto"]
    chain: list = []
    for it in autos:
        if chain and it.event.start < chain[-1].event.end:
            it.status = "ueberschneidung"
            it.detail = T("planner.overlaps", title=chain[-1].event.title)
            notices.append(Notice("overlap:%s:%s" % (chain[0].event.uid, it.event.uid),
                                  T("planner.notice.overlap", kept=chain[-1].event.title, ignored=it.event.title)))
            continue
        if chain:
            gap_h = (it.ready_by - chain[-1].back_home).total_seconds() / 3600.0
            if gap_h > cfg.chain_window_h:
                it.status, it.detail = "spaeter", T("planner.later", h=_g(cfg.chain_window_h))
                continue
        chain.append(it)
    if not chain:
        ev.skipped_reason = T("planner.no_plannable")
        return ev

    # 3. work backwards: required charge level at departure for each event.
    #    A given target applies as-is; without computed demand, (target - reserve) counts as driving demand.
    def fixed(it):
        return it.override.target if it.override and it.override.target is not None else None

    def trip_need(it):
        return it.need_soc if it.need_soc is not None else max(0.0, fixed(it) - cfg.reserve_soc)

    req = [0.0] * len(chain)
    for k in range(len(chain) - 1, -1, -1):
        if fixed(chain[k]) is not None:
            req[k] = float(fixed(chain[k]))
            continue
        base = chain[k].need_soc + cfg.reserve_soc
        if k == len(chain) - 1:
            req[k] = base
        else:
            gap_h = (chain[k + 1].ready_by - chain[k].back_home).total_seconds() / 3600.0
            gain = calc.charge_gain_soc(gap_h, capacity_kwh, cfg.charge_power_kw, cfg.charge_loss_margin)
            req[k] = max(base, req[k + 1] - gain + trip_need(chain[k]))
    for k, it in enumerate(chain):
        it.target_chain = int(fixed(it)) if fixed(it) is not None else min(100, calc.ceil5(req[k]))

    first = chain[0]
    soc = first.target_chain
    over100 = fixed(first) is None and req[0] > 100.0 + 1e-6
    capped_from = None
    if over100:
        notices.append(Notice("over100:%s:%d" % (first.event.uid, int(req[0])),
                              T("planner.notice.over100", title=first.event.title, need=round(req[0]))))
    unclear = [i for i in chain if i.cls and i.cls.unclear]
    if unclear and fixed(first) is None and soc > cfg.unclear_cap_soc:
        capped_from = soc
        soc = cfg.unclear_cap_soc
        notices.append(Notice("unclear:%s:%d" % (first.event.uid, capped_from),
                              T("planner.notice.unclear", soc=soc, needed=capped_from, title=first.event.title)))
    if soc <= min_soc:
        first.status = "unter_min"
        first.detail = T("planner.below_min", soc=soc, min=_g(min_soc))
        ev.skipped_reason = first.detail
        return ev
    first.status = "geplant"
    ev.desired = Desired(item=first, soc=int(soc), time=first.ready_by, capped_from=capped_from, over100=over100)
    for it in chain[1:]:
        it.status = "verkettet"
        it.detail = T("planner.chained", soc=it.target_chain)
    return ev


def _target(need: float, reserve: float) -> tuple:
    raw = need + reserve
    return min(100, calc.ceil5(raw)), raw > 100.0


# ---------------------------------------------------------------- Context notices
def next_occurrence(plan: dict, after: datetime, before: datetime) -> Optional[datetime]:
    """Next occurrence of a repeating plan in the window (after, before]."""
    try:
        tz = ZoneInfo(plan.get("tz") or "UTC")
        hh, mm = [int(x) for x in str(plan["time"]).split(":")[:2]]
    except Exception:
        return None
    days = plan.get("weekdays") or []
    d0 = after.astimezone(tz).date()
    d1 = before.astimezone(tz).date()
    best = None
    d = d0
    while d <= d1:
        if (d.isoweekday() % 7) in days:              # evcc: 0 = Sunday
            cand = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz).astimezone(timezone.utc)
            if after < cand <= before and (best is None or cand < best):
                best = cand
        d += timedelta(days=1)
    return best


def context_notices(desired: Desired, repeating_plans: list, vehicle_limit_soc: Optional[float],
                    cfg, capacity_kwh: float, now: datetime) -> list:
    T = _translator(getattr(cfg, "language", "en"))
    out = []
    t = desired.item.event.title
    # Vehicle limit
    if vehicle_limit_soc is not None and 0 < vehicle_limit_soc < cfg.vehicle_limit_min:
        out.append(Notice("carlimit:%s:%d" % (desired.item.event.uid, int(vehicle_limit_soc)),
                          T("planner.notice.car_limit", limit=int(vehicle_limit_soc), title=t, soc=desired.soc)))
    # planning too tight with active repeating plans before it
    for p in repeating_plans or []:
        if not p.get("active"):
            continue
        occ = next_occurrence(p, now, desired.time)
        if occ is None or occ >= desired.time:
            continue
        soc_rep = float(p.get("soc") or 0)
        if soc_rep >= desired.soc:
            continue
        need_h = calc.hours_to_charge(desired.soc - soc_rep, capacity_kwh, cfg.charge_power_kw, cfg.charge_loss_margin)
        gap_h = (desired.time - occ).total_seconds() / 3600.0
        if need_h > gap_h:
            out.append(Notice("tight:%s:%s" % (desired.item.event.uid, occ.strftime("%Y%m%d%H%M")),
                              T("planner.notice.tight", occ=_fmt(occ, cfg.timezone), rep_soc=int(soc_rep), title=t,
                            soc=desired.soc, time=_fmt(desired.time, cfg.timezone),
                            need="%.1f" % need_h, gap="%.1f" % gap_h)))
    return out


# ---------------------------------------------------------------- Reconciliation with evcc
@dataclass
class Action:
    kind: str          # set | delete | none | manual
    reason: str = ""


def _parse(ts) -> Optional[datetime]:
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return None


def _same(plan: Optional[dict], soc: int, time: datetime) -> bool:
    if not plan:
        return False
    pt = _parse(plan.get("time"))
    return bool(pt) and plan.get("soc") == soc and abs((pt - time).total_seconds()) < 60


def reconcile(desired: Optional[Desired], current: Optional[dict], last_set: Optional[dict],
              now: datetime, lang: str = "en") -> Action:
    """current = vehicles.<n>.plan from evcc, last_set = {'soc','time'} from our store."""
    T = _translator(lang)
    cur_time = _parse(current.get("time")) if current else None
    cur_future = bool(current) and cur_time is not None and cur_time > now
    ours = bool(current) and bool(last_set) and _same(current, int(last_set["soc"]), _parse(last_set["time"]))
    if desired is None:
        if ours and cur_future:
            return Action("delete", T("planner.act.delete"))
        return Action("none", T("planner.act.none_needed"))
    if not current or not cur_future:
        return Action("set", T("planner.act.set_new"))
    if _same(current, desired.soc, desired.time):
        return Action("none", T("planner.act.same"))
    if ours:
        return Action("set", T("planner.act.update"))
    return Action("manual", T("planner.act.manual"))
