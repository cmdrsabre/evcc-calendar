"""Charging window, progress against the plan and cost forecast for the next plan. Pure logic, no network.

extract(): reads the values needed from an evcc state (defensive, anything missing is left out).
build(): turns them into the view shown in the UI; the poller calls it with the cached values.
max_cost(): upper bound of the grid cost of charging (no PV share, losses included).
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone
from typing import Optional

from . import calc, planner

ON_PLAN_MIN = 5            # |difference| below this counts as "on plan"
DEFAULT_CURRENCY = "EUR"
MAX_PRICE = 5.0            # per kWh; anything above is a unit mix-up (cents), not a price


def _num(v) -> Optional[float]:
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v):
        return None
    return float(v)


def _time(v) -> Optional[datetime]:
    if isinstance(v, (int, float)) and not isinstance(v, bool):      # unix seconds (evcc forecast slots)
        try:
            return datetime.fromtimestamp(v, timezone.utc)
        except (OverflowError, OSError, ValueError):
            return None
    dt = planner._parse(v)
    return dt if dt and dt.year >= 2000 else None             # evcc reports "no time" as year 1


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return None if dt is None else dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _rates(items) -> list:
    """Grid forecast slots as dicts; evcc sends [start, end, value] (value in currency per kWh), objects are accepted too."""
    out = []
    for s in items if isinstance(items, list) else []:
        if isinstance(s, dict):
            a, b, v = _time(s.get("start")), _time(s.get("end")), _num(s.get("value"))
        elif isinstance(s, (list, tuple)) and len(s) >= 3:
            a, b, v = _time(s[0]), _time(s[1]), _num(s[2])
        else:
            continue
        if a and b and b > a and v is not None:
            out.append({"start": a, "end": b, "value": v})
    return out


def _mean_price(rates: list, windows: list) -> Optional[float]:
    """Time-weighted mean of the forecast price over the given (start, end) windows."""
    total = weighted = 0.0
    for a, b in windows:
        for r in rates:
            sec = (min(r["end"], b) - max(r["start"], a)).total_seconds()
            if sec > 0:
                total += sec
                weighted += r["value"] * sec
    return weighted / total if total > 0 else None


def price_of(state: dict, windows: Optional[list] = None) -> tuple:
    """(price per kWh, currency, source) or (None, None, None). Never an estimate: without evcc price, no price.

    All prices are the grid tariff in currency per kWh (tariffGrid, forecast.grid). The plan slot values in the
    loadpoint plan are in cents and are not used. With windows (the slots the car will charge in) the forecast
    over those windows counts, otherwise the current grid price.
    """
    cur = state.get("currency") if isinstance(state.get("currency"), str) else DEFAULT_CURRENCY
    fc = state.get("forecast") if isinstance(state.get("forecast"), dict) else {}
    p = _mean_price(_rates(fc.get("grid")), windows) if windows else None
    if p is not None and 0 <= p <= MAX_PRICE:
        return p, cur, "forecast"
    p = _num(state.get("tariffGrid"))
    if p is None or not 0 <= p <= MAX_PRICE:                    # not a plausible price per kWh: show no cost rather than nonsense
        return None, None, None
    return p, cur, "grid"


def _solar(state: dict) -> tuple:
    """(series [(time, kW)], scale) from forecast.solar; evcc sends the timeseries as [unix time, W]."""
    fc = state.get("forecast") if isinstance(state.get("forecast"), dict) else {}
    sol = fc.get("solar") if isinstance(fc.get("solar"), dict) else {}
    series = []
    for e in sol.get("timeseries") if isinstance(sol.get("timeseries"), list) else []:
        if isinstance(e, (list, tuple)) and len(e) >= 2:
            t, w = _time(e[0]), _num(e[1])
            if t and w is not None:
                series.append((t, max(0.0, w) / 1000.0))
    scale = _num(sol.get("scale"))
    return sorted(series), min(1.0, scale) if scale is not None and scale > 0 else 1.0    # only ever reduce the forecast


def _pv_kw(series: list, at: datetime) -> float:
    """Interpolated forecast power; 0 outside the forecast."""
    if len(series) < 2 or at < series[0][0] or at > series[-1][0]:
        return 0.0
    for (t0, v0), (t1, v1) in zip(series, series[1:]):
        if t0 <= at <= t1:
            span = (t1 - t0).total_seconds()
            return v0 if span <= 0 else v0 + (v1 - v0) * (at - t0).total_seconds() / span
    return 0.0


def pv_energy(segments: list, series: list, scale: float, home_kw: float, charge_kw: float,
              not_before: Optional[datetime] = None) -> float:
    """Grid energy (kWh) the PV surplus is expected to cover while charging in the segments (no feed-in value, no battery)."""
    step, total = timedelta(minutes=5), 0.0
    for a, b in segments:
        t = max(a, not_before) if not_before else a
        while t < b:
            dt = min(step, b - t)
            surplus = max(0.0, _pv_kw(series, t + dt / 2) * scale - home_kw)
            total += min(charge_kw, surplus) * dt.total_seconds() / 3600.0
            t += dt
    return total


def add_pv(cost: Optional[dict], segments: list, state: dict, cfg, not_before: Optional[datetime] = None) -> None:
    """Adds the expected PV share: cost["pv_kwh"] and cost["est"] (grid part only); "max" stays the figure without PV."""
    if not cost:
        return
    series, scale = _solar(state)
    home = _num(state.get("homePower"))
    pv = pv_energy(segments, series, scale, (home or 0.0) / 1000.0 if home is not None else 0.5, cfg.charge_power_kw, not_before)
    pv = min(pv, cost["kwh"])
    if pv >= 0.05:
        cost["pv_kwh"] = round(pv, 1)
        cost["est"] = round(max(0.0, cost["kwh"] - pv) * cost["price"], 2)


def max_cost(target: float, start_soc: float, capacity_kwh: float, loss_margin: float,
             price: Optional[float]) -> Optional[dict]:
    """Upper bound: battery energy from start to target plus charging losses, all at the grid price."""
    if price is None or not capacity_kwh or capacity_kwh <= 0:
        return None
    batt = max(0.0, target - start_soc) / 100.0 * capacity_kwh
    grid = batt / (1.0 - loss_margin) if loss_margin < 1 else batt
    return {"kwh": round(grid, 1), "price": round(price, 4), "max": round(grid * price, 2)}


def _slots(lp: dict) -> list:
    out = []
    for s in lp.get("plan") or []:
        if not isinstance(s, dict):
            continue
        a, b = _time(s.get("start")), _time(s.get("end"))
        if a and b and b > a:
            out.append({"start": a, "end": b})
    return sorted(out, key=lambda x: x["start"])


def _plan(state: dict, lp: dict, veh: dict, now: datetime) -> Optional[dict]:
    """The plan that decides the next charge: evcc's effective plan, else the earliest one set for the vehicle."""
    t, soc = _time(lp.get("effectivePlanTime")), _num(lp.get("effectivePlanSoc"))
    if t and soc is not None:
        return {"time": t, "soc": soc}
    cands = []
    p = veh.get("plan") or {}
    pt, ps = _time(p.get("time")), _num(p.get("soc"))
    if pt and ps is not None and pt > now:
        cands.append({"time": pt, "soc": ps})
    for rp in veh.get("repeatingPlans") or []:
        if isinstance(rp, dict) and rp.get("active") and _num(rp.get("soc")) is not None:
            occ = planner.next_occurrence(rp, now, now + timedelta(days=7))
            if occ:
                cands.append({"time": occ, "soc": float(rp["soc"])})
    return min(cands, key=lambda c: c["time"]) if cands else None


def extract(state, vehicle: str, now: datetime, loadpoint: Optional[dict] = None) -> dict:
    """Raw values for build(); empty when there is no plan or no vehicle capacity."""
    if not isinstance(state, dict):
        return {}
    vehicles = state.get("vehicles") if isinstance(state.get("vehicles"), dict) else {}
    name = vehicle or (next(iter(vehicles)) if len(vehicles) == 1 else "")
    veh = vehicles.get(name) if isinstance(vehicles.get(name), dict) else {}
    lp = loadpoint if loadpoint is not None else {}
    cap = _num(veh.get("capacity"))
    plan = _plan(state, lp, veh, now)
    if not cap or not plan:
        return {}
    slots = _slots(lp)
    price, currency, source = price_of(state, [(s["start"], s["end"]) for s in slots])
    return {"capacity": cap, "plan": plan, "connected": lp.get("connected") is True,
            "charging": lp.get("charging") is True, "soc": _num(lp.get("vehicleSoc")),
            "charged_kwh": (_num(lp.get("chargedEnergy")) or 0.0) / 1000.0,         # this session, from the grid side (Wh in evcc)
            "start": _time(lp.get("planProjectedStart")), "end": _time(lp.get("planProjectedEnd")),
            "evcc": state, "slots": slots, "grid_slots": _rates((state.get("forecast") or {}).get("grid")
                                                 if isinstance(state.get("forecast"), dict) else None), "price": price, "currency": currency, "price_source": source}


def build(raw: dict, cfg, now: datetime) -> Optional[dict]:
    if not raw:
        return None
    cap, target = raw["capacity"], raw["plan"]["soc"]
    deadline = raw["plan"]["time"]
    connected, soc = raw["connected"], raw["soc"]
    known = connected and soc is not None
    start_soc = soc if known else float(cfg.reserve_soc)         # away: assume the car returns at the reserve level
    margin, power = cfg.charge_loss_margin, cfg.charge_power_kw
    rate = power * (1.0 - margin) / cap * 100.0                    # percentage points per hour into the battery
    need_h = calc.hours_to_charge(target - start_soc, cap, power, margin)

    if raw["start"] and raw["end"] and connected:
        w_start, w_end, estimated = raw["start"], raw["end"], False
    else:
        w_start, w_end, estimated = deadline - timedelta(hours=need_h), deadline, True
    segments = [{"start": _iso(s["start"]), "end": _iso(s["end"])} for s in raw["slots"]] if raw["slots"] and connected \
        else [{"start": _iso(w_start), "end": _iso(w_end)}]

    done = known and soc >= target
    if done:
        state = "done"
    elif not connected:
        state = "away"
    elif raw["charging"]:
        state = "charging"
    else:
        state = "waiting"

    progress = None
    if known and not done and now >= w_start:
        # time left until the plan time minus the time still needed at the planned rate: > 0 ahead, < 0 behind
        left_h = (deadline - now).total_seconds() / 3600.0
        slack_min = round((left_h - calc.hours_to_charge(target - soc, cap, power, margin)) * 60)
        kind = "on" if abs(slack_min) < ON_PLAN_MIN else ("ahead" if slack_min > 0 else "behind")
        progress = {"kind": kind, "minutes": abs(slack_min),
                    "expected_soc": round(min(100.0, max(0.0, soc - slack_min / 60.0 * rate)), 1)}   # where it should be right now

    price, source = raw["price"], raw["price_source"]
    if source == "grid" and estimated:                              # no plan slots yet: forecast over the estimated window
        fc = _mean_price(raw["grid_slots"], [(w_start, w_end)])
        if fc is not None:
            price, source = fc, "forecast"
    # what the whole charge costs, not what is left: while charging, go back to the state of charge at the start of the plan
    plan_start_soc = start_soc
    if state == "charging" and now >= w_start and raw["charged_kwh"] > 0:
        plan_start_soc = max(0.0, soc - raw["charged_kwh"] * (1.0 - margin) / cap * 100.0)
    cost = None if done else max_cost(target, plan_start_soc, cap, margin, price)
    if cost:
        cost.update({"currency": raw["currency"], "source": source, "estimated_start": not known})
        add_pv(cost, [(s["start"], s["end"]) for s in raw["slots"]] if raw["slots"] and connected else [(w_start, w_end)],
               raw["evcc"], cfg)
    return {"state": state, "target": target, "soc": soc, "start_soc": round(start_soc, 1), "deadline": _iso(deadline),
            "window": {"start": _iso(w_start), "end": _iso(w_end), "estimated": estimated,
                       "hours": round(calc.hours_to_charge(target - plan_start_soc, cap, power, margin), 2), "segments": segments},
            "progress": progress, "cost": cost, "rate_pct_h": round(rate, 1)}


def estimate_view(target: float, start_soc: float, ready_by: datetime, capacity: float, cfg, state: dict,
                  earliest: Optional[datetime] = None, estimated_start: bool = True) -> Optional[dict]:
    """The same view shape as build() for a charge that is planned but not under way yet (calendar item).

    The window ends at `ready_by`; it starts as late as possible but not before `earliest` (e.g. when the car is back home).
    """
    need_h = calc.hours_to_charge(target - start_soc, capacity, cfg.charge_power_kw, cfg.charge_loss_margin)
    if need_h <= 0:
        return None
    start = ready_by - timedelta(hours=need_h)
    if earliest and start < earliest:
        start = earliest
    price, currency, source = price_of(state, [(start, ready_by)])
    if source == "grid":
        fc = _mean_price(_rates((state.get("forecast") or {}).get("grid") if isinstance(state.get("forecast"), dict) else None),
                         [(start, ready_by)])
        if fc is not None and 0 <= fc <= MAX_PRICE:
            price, source = fc, "forecast"
    cost = max_cost(target, start_soc, capacity, cfg.charge_loss_margin, price)
    if cost:
        cost.update({"currency": currency, "source": source, "estimated_start": estimated_start})
        add_pv(cost, [(start, ready_by)], state, cfg)
    return {"state": "planned", "target": target, "soc": None, "start_soc": round(start_soc, 1), "deadline": _iso(ready_by),
            "window": {"start": _iso(start), "end": _iso(ready_by), "estimated": True, "hours": round(need_h, 2),
                       "segments": [{"start": _iso(start), "end": _iso(ready_by)}]},
            "progress": None, "cost": cost, "rate_pct_h": None}
