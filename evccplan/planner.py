"""Reine Planungslogik ohne Netzwerk.

evaluate(): Termine -> gewuenschter Plan (Ziel-SoC + Zeit) und Hinweise.
context_notices(): Hinweise, die den evcc-Zustand brauchen.
reconcile(): gewuenschter Plan gegen den vorhandenen Plan in evcc.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable, Optional
from zoneinfo import ZoneInfo

from . import calc
from .classify import classify
from .models import Desired, Evaluation, Item, Mode, Notice, Override, Route, override_key

Resolver = Callable[[object], tuple]          # Event -> (Route|None, Fehlertext|None, Ortsname)
TempAt = Callable[[datetime], Optional[float]]


def floor5min(dt: datetime) -> datetime:
    dt = dt.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return dt - timedelta(minutes=dt.minute % 5)


def _fmt(dt: datetime, tz: str) -> str:
    return dt.astimezone(ZoneInfo(tz)).strftime("%a %d.%m. %H:%M")


def evaluate(events: list, now: datetime, cfg, capacity_kwh: float, min_soc: float,
             resolve: Resolver, temp_at: TempAt, overrides: Optional[dict] = None) -> Evaluation:
    """overrides: {override_key(event): Override} aus der Oberflaeche."""
    ev = Evaluation()
    notices = ev.notices
    overrides = overrides or {}
    manual_drive = timedelta(minutes=cfg.manual_drive_min)
    # 1. je Termin einstufen, Strecke und Bedarf bestimmen
    for e in sorted(events, key=lambda x: x.start):
        it = Item(event=e)
        ev.items.append(it)
        ov = overrides.get(override_key(e)) or Override()
        it.override = ov if (ov.mode or ov.target is not None) else None
        has_loc = bool((e.location or "").strip())
        if ov.mode == "none":
            it.status, it.detail = "manuell_aus", "per Oberfläche: kein Auto"
            continue
        # Auto ist gesetzt, wenn "Auto" gewaehlt oder ein Ziel vorgegeben wurde (ein Ziel heisst: hier wird geladen)
        forced = ov.mode == "car" or ov.target is not None
        if not has_loc and not forced:
            it.status, it.detail = "kein_ort", "Termin ohne Ort wird ignoriert"
            continue
        if has_loc:
            it.cls = classify(e.title, e.description, cfg.rules)
            if it.cls.mode == Mode.BAHN and not forced:
                it.status, it.detail = "bahn", it.cls.reason
                continue
            route, err, label = resolve(e)
            it.place_label = label or ""
        else:
            route, err = None, "kein Ort angegeben"
        if route is None:
            if ov.target is None:
                if has_loc:
                    it.status, it.detail = "adresse_unklar", err or "Adresse nicht auflösbar"
                    if not (err or "").startswith("ORS nicht erreichbar"):     # Ausfall wird einmal gesammelt gemeldet
                        notices.append(Notice("addr:%s" % e.uid,
                                              "Adresse unklar bei '%s' (%s). Für diesen Termin gibt es keinen Ladeplan."
                                              % (e.title, it.detail)))
                else:
                    it.status, it.detail = "ziel_fehlt", "Auto ohne Ort: Ziel manuell wählen"
                    notices.append(Notice("notarget:%s" % e.uid,
                                          "'%s' ist als Auto-Termin markiert, hat aber keinen Ort. Wähle in der "
                                          "Oberfläche ein Ziel, sonst gibt es keinen Ladeplan." % e.title))
                continue
            # Strecke unbekannt, aber Ziel vorgegeben: mit angenommener Fahrzeit planen
            it.manual = True
            drive = manual_drive
            it.detail = "Ziel manuell, Fahrzeit angenommen (%g min)" % cfg.manual_drive_min
            if has_loc:
                it.temp_c = temp_at(e.start)
        else:
            it.route = route
            if route.distance_km < cfg.min_car_km and not forced and not (it.cls and it.cls.explicit):
                it.status, it.detail = "nah", "unter %g km: zu Fuß/Rad, kein Auto-Termin" % cfg.min_car_km
                continue
            it.temp_c = temp_at(e.start)
            it.kwh100 = calc.kwh_per_100(it.temp_c, cfg.warm, cfg.cold, cfg.temp_threshold_c)
            it.need_soc = calc.round_trip_need_soc(route.distance_km, it.kwh100, capacity_kwh)
            it.target_alone, _ = _target(it.need_soc, cfg.reserve_soc)
            drive = timedelta(minutes=route.duration_min)
            if route.estimated:
                it.detail = "Luftlinie x 1,3 statt Routing"
        it.ready_by = floor5min(e.start - drive - timedelta(minutes=cfg.time_buffer_min))
        it.back_home = e.end + drive
        if it.ready_by <= now:
            it.status, it.detail = "zu_spaet", "Abfahrtszeit liegt in der Vergangenheit"
            continue
        it.status = "auto"

    # 2. Kette der Auto-Termine, Ueberschneidungen ausschliessen
    autos = [i for i in ev.items if i.status == "auto"]
    chain: list = []
    for it in autos:
        if chain and it.event.start < chain[-1].event.end:
            it.status = "ueberschneidung"
            it.detail = "überschneidet sich mit '%s'" % chain[-1].event.title
            notices.append(Notice("overlap:%s:%s" % (chain[0].event.uid, it.event.uid),
                                  "Zwei Auto-Termine überschneiden sich: '%s' gilt, '%s' wird ignoriert."
                                  % (chain[-1].event.title, it.event.title)))
            continue
        if chain:
            gap_h = (it.ready_by - chain[-1].back_home).total_seconds() / 3600.0
            if gap_h > cfg.chain_window_h:
                it.status, it.detail = "spaeter", "liegt mehr als %g h nach dem vorigen Auto-Termin" % cfg.chain_window_h
                continue
        chain.append(it)
    if not chain:
        ev.skipped_reason = "Kein planbarer Auto-Termin im Zeitraum"
        return ev

    # 3. rueckwaerts rechnen: benoetigter Ladestand bei Abfahrt je Termin.
    #    Ein vorgegebenes Ziel gilt genau so; ohne berechneten Bedarf zaehlt (Ziel - Reserve) als Fahrbedarf.
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
                              "Bedarf für '%s' liegt bei etwa %d %% und damit über 100 %%. Plan auf 100 %%, "
                              "die Fahrt wird knapp oder braucht Laden unterwegs." % (first.event.title, round(req[0]))))
    unclear = [i for i in chain if i.cls and i.cls.unclear]
    if unclear and fixed(first) is None and soc > cfg.unclear_cap_soc:
        capped_from = soc
        soc = cfg.unclear_cap_soc
        notices.append(Notice("unclear:%s:%d" % (first.event.uid, capped_from),
                              "Einstufung unklar: Plan nur auf %d %% statt der sonst nötigen %d %% ('%s')."
                              % (soc, capped_from, first.event.title)))
    if soc <= min_soc:
        first.status = "unter_min"
        first.detail = "Bedarf %d %% liegt unter dem evcc-Minimum von %g %%" % (soc, min_soc)
        ev.skipped_reason = first.detail
        return ev
    first.status = "geplant"
    ev.desired = Desired(item=first, soc=int(soc), time=first.ready_by, capped_from=capped_from, over100=over100)
    for it in chain[1:]:
        it.status = "verkettet"
        it.detail = "Teil der Kette, Ziel %d %%" % it.target_chain
    return ev


def _target(need: float, reserve: float) -> tuple:
    raw = need + reserve
    return min(100, calc.ceil5(raw)), raw > 100.0


# ---------------------------------------------------------------- Kontexthinweise
def next_occurrence(plan: dict, after: datetime, before: datetime) -> Optional[datetime]:
    """Naechster Zeitpunkt eines Wiederholplans im Fenster (after, before]."""
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
        if (d.isoweekday() % 7) in days:              # evcc: 0 = Sonntag
            cand = datetime(d.year, d.month, d.day, hh, mm, tzinfo=tz).astimezone(timezone.utc)
            if after < cand <= before and (best is None or cand < best):
                best = cand
        d += timedelta(days=1)
    return best


def context_notices(desired: Desired, repeating_plans: list, vehicle_limit_soc: Optional[float],
                    cfg, capacity_kwh: float, now: datetime) -> list:
    out = []
    t = desired.item.event.title
    # Fahrzeug-Limit
    if vehicle_limit_soc is not None and 0 < vehicle_limit_soc < cfg.vehicle_limit_min:
        out.append(Notice("carlimit:%s:%d" % (desired.item.event.uid, int(vehicle_limit_soc)),
                          "Das Ladelimit im Auto steht auf %d %%. Für '%s' ist ein Ziel von %d %% geplant, "
                          "das Auto bricht sonst früher ab." % (vehicle_limit_soc, t, desired.soc)))
    # zu enge Planung mit aktiven Wiederholplaenen davor
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
                              "Zu enge Planung: Wiederholplan %s auf %d %%, danach '%s' auf %d %% um %s. "
                              "Dafür braucht es etwa %.1f h, es bleiben %.1f h. evcc zeigt nur den früheren Zeitpunkt."
                              % (_fmt(occ, cfg.timezone), soc_rep, t, desired.soc,
                                 _fmt(desired.time, cfg.timezone), need_h, gap_h)))
    return out


# ---------------------------------------------------------------- Abgleich mit evcc
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
              now: datetime) -> Action:
    """current = vehicles.<n>.plan aus evcc, last_set = {'soc','time'} aus unserem Speicher."""
    cur_time = _parse(current.get("time")) if current else None
    cur_future = bool(current) and cur_time is not None and cur_time > now
    ours = bool(current) and bool(last_set) and _same(current, int(last_set["soc"]), _parse(last_set["time"]))
    if desired is None:
        if ours and cur_future:
            return Action("delete", "Plan stammt vom Dienst, passender Termin existiert nicht mehr")
        return Action("none", "kein Plan nötig")
    if not current or not cur_future:
        return Action("set", "kein aktueller Plan in evcc")
    if _same(current, desired.soc, desired.time):
        return Action("none", "Plan entspricht bereits dem Soll")
    if ours:
        return Action("set", "eigener Plan wird aktualisiert")
    return Action("manual", "in evcc steht ein manuell gesetzter Plan")
