"""Live status: a background thread polls evcc /api/state and keeps the last result in memory.

The poller only reads from evcc. It has no access to the runner, the calendar, Home Assistant or
OpenRouteService, so a status poll can never start a planning run or a geocoding request.
"""
from __future__ import annotations

import logging
import math
import threading
from datetime import datetime, timezone
from typing import Callable, Optional

from . import chargeview
from .clients import ApiError

log = logging.getLogger("evccplan")

MIN_INTERVAL_S, MAX_INTERVAL_S, DEFAULT_INTERVAL_S = 5, 300, 10


def clamp_interval(value) -> int:
    """Poll interval in seconds within [MIN_INTERVAL_S, MAX_INTERVAL_S]; invalid input gives the default."""
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return DEFAULT_INTERVAL_S
    return int(min(MAX_INTERVAL_S, max(MIN_INTERVAL_S, round(value))))


def _number(value) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _kilo(value) -> Optional[float]:
    n = _number(value)
    return None if n is None else round(n / 1000.0, 2)


def _flag(value) -> Optional[bool]:
    return value if isinstance(value, bool) else None


def _loadpoint(state: dict, vehicle: str) -> dict:
    lps = [lp for lp in state.get("loadpoints") or [] if isinstance(lp, dict)]
    match = [lp for lp in lps if vehicle and lp.get("vehicleName") == vehicle]
    if match:
        return match[0]
    return lps[0] if len(lps) == 1 else {}


def _grid_power(state: dict) -> Optional[float]:
    grid = state.get("grid")
    if isinstance(grid, dict):
        return _number(grid.get("power"))
    return _number(state.get("gridPower"))        # older evcc versions (not verified)


def parse_state(state, vehicle: str = "") -> dict:
    """Extracts the display values from an evcc state. Fields that are missing or of the wrong type are left out.

    evcc reports power in W and the charged energy in Wh; the result uses kW and kWh.
    Grid power is positive when importing, negative when feeding in.
    """
    if not isinstance(state, dict):
        return {"loadpoint": {}, "site": {}}
    lp = _loadpoint(state, vehicle)
    soc = _number(lp.get("vehicleSoc"))
    energy = _kilo(lp.get("chargedEnergy"))
    loadpoint = {"connected": _flag(lp.get("connected")), "charging": _flag(lp.get("charging")),
                 "power_kw": _kilo(lp.get("chargePower")), "soc": None if soc is None else round(soc, 1),
                 "energy_kwh": energy}
    site = {"pv_kw": _kilo(state.get("pvPower")), "grid_kw": _kilo(_grid_power(state)),
            "home_kw": _kilo(state.get("homePower"))}
    return {"loadpoint": {k: v for k, v in loadpoint.items() if v is not None},
            "site": {k: v for k, v in site.items() if v is not None}}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class LivePoller:
    def __init__(self, evcc, cfg, now_fn: Callable[[], datetime] = lambda: datetime.now(timezone.utc)):
        self.evcc, self.cfg, self.now_fn = evcc, cfg, now_fn
        self._lock = threading.Lock()
        self._snap: dict = {"reachable": None, "time": None, "ok_time": None, "loadpoint": {}, "site": {}}
        self._solar_hist: dict = {}                                # forecast values of passed quarter hours (poll thread only)
        self._raw: dict = {}                                       # values for the charge view, from the last good poll
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    @property
    def interval_s(self) -> int:
        return clamp_interval(getattr(self.cfg, "live_poll_s", DEFAULT_INTERVAL_S))

    def next_wait_s(self, fails: int) -> float:
        """Interval after the last poll; doubles with every failure (up to 5 min) so a struggling evcc is left alone."""
        return min(MAX_INTERVAL_S, self.interval_s * 2 ** min(fails, 6))

    def snapshot(self) -> dict:
        """The cached result of the last poll (no request to evcc)."""
        with self._lock:
            snap = {**self._snap, "loadpoint": dict(self._snap["loadpoint"]), "site": dict(self._snap["site"]),
                    "interval_s": self.interval_s}
            raw = self._raw
        snap["charge"] = None
        if snap["reachable"] and raw:
            try:
                snap["charge"] = chargeview.build(raw, self.cfg, self.now_fn())
            except Exception:                                      # the view must never break the live status
                log.exception("Charge view failed")
        return snap

    def poll_once(self) -> None:
        now = _iso(self.now_fn())
        try:
            state = self.evcc.state()
            parsed = parse_state(state, self.cfg.evcc_vehicle)
            raw = self._extract(state)
            new = {"reachable": True, "time": now, "ok_time": now, **parsed}
        except ApiError as exc:
            new = self._failed(now, str(exc))
        except Exception as exc:                                   # the poll must never stop the thread
            log.exception("Live status poll failed")
            new = self._failed(now, str(exc))
        with self._lock:
            was = self._snap["reachable"]
            if new["reachable"] is False:
                new["ok_time"] = self._snap["ok_time"]
            else:
                self._raw = raw
            self._snap = new
        if new["reachable"] and was is False:
            log.info("Live status: evcc is reachable again")

    def _extract(self, state) -> dict:
        try:
            lp = _loadpoint(state, self.cfg.evcc_vehicle) if isinstance(state, dict) else {}
            return chargeview.extract(state, self.cfg.evcc_vehicle, self.now_fn(), lp, self._solar_hist)
        except Exception:                                          # a odd state must not mark evcc as unreachable
            log.exception("Charge view: state not readable")
            return {}

    def _failed(self, now: str, detail: str) -> dict:
        with self._lock:
            was = self._snap["reachable"]
        if was is not False:                                       # log the change, not every poll
            log.warning("Live status: evcc not reachable (%s)", detail)
        return {"reachable": False, "time": now, "ok_time": None, "loadpoint": {}, "site": {}}

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="live-status", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        fails = 0
        while True:
            self.poll_once()
            fails = fails + 1 if self._snap["reachable"] is False else 0
            if self._stop.wait(self.next_wait_s(fails)):
                return
