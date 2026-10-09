from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

import yaml

from .classify import Rule
from .models import Mode


@dataclass
class Config:
    home_lat: float = 0.0
    home_lon: float = 0.0
    home_address: str = ""
    ha_url: str = ""
    ha_token: str = ""
    ha_calendar: str = "calendar.a_a"
    notify_targets: dict = field(default_factory=dict)   # level -> notify entities
    ha_weather: str = ""
    evcc_url: str = ""
    evcc_key: str = ""
    evcc_vehicle: str = ""
    ors_key: str = ""
    ors_min_confidence: float = 0.7
    warm: float = 20.0
    cold: float = 23.0
    temp_threshold_c: float = 10.0
    reserve_soc: float = 10.0
    unclear_cap_soc: int = 80
    min_car_km: float = 4.0
    manual_drive_min: float = 45.0   # assumed travel time for events without a computed route
    time_buffer_min: float = 30.0
    charge_power_kw: float = 11.0
    charge_loss_margin: float = 0.15
    chain_window_h: float = 5.0
    horizon_days: int = 6
    run_every_min: int = 30
    vehicle_limit_min: int = 100
    rules: list = field(default_factory=list)
    dry_run: bool = True
    web_host: str = "0.0.0.0"
    web_port: int = 80
    db_path: str = "/data/state.db"
    personal_dir: str = ""           # personal images (not in the repo); default: "personal" folder next to the database
    timezone: str = "Europe/Berlin"
    retries: int = 3
    retry_delay_s: float = 5.0

    @property
    def personal_path(self) -> str:
        return self.personal_dir or os.path.join(os.path.dirname(self.db_path) or ".", "personal")

    @property
    def home_known(self) -> bool:
        return not (self.home_lat == 0.0 and self.home_lon == 0.0)


def _env(name: Optional[str]) -> str:
    return os.environ.get(name or "", "") if name else ""


def from_dict(d: dict, env=_env) -> Config:
    c = Config()
    home = d.get("home") or {}
    c.home_lat = float(home.get("lat") or 0.0)
    c.home_lon = float(home.get("lon") or 0.0)
    c.home_address = home.get("address") or ""
    ha = d.get("ha") or {}
    c.ha_url = (ha.get("url") or "").rstrip("/")
    c.ha_token = env(ha.get("token_env"))
    c.ha_calendar = ha.get("calendar") or c.ha_calendar
    tg = ha.get("notify_targets") or {}
    for lvl in ("info", "warning", "error"):
        v = tg.get(lvl) or []
        c.notify_targets[lvl] = [v] if isinstance(v, str) else list(v)
    c.ha_weather = ha.get("weather") or ""
    ev = d.get("evcc") or {}
    c.evcc_url = (ev.get("url") or "").rstrip("/")
    c.evcc_key = env(ev.get("api_key_env"))
    c.evcc_vehicle = ev.get("vehicle") or ""
    ors = d.get("ors") or {}
    c.ors_key = env(ors.get("key_env"))
    c.ors_min_confidence = float(ors.get("min_confidence", c.ors_min_confidence))
    cons = d.get("consumption") or {}
    c.warm = float(cons.get("warm", c.warm))
    c.cold = float(cons.get("cold", c.cold))
    c.temp_threshold_c = float(cons.get("temp_threshold_c", c.temp_threshold_c))
    for key in ("reserve_soc", "time_buffer_min", "charge_power_kw",
                "charge_loss_margin", "chain_window_h", "retry_delay_s", "min_car_km", "manual_drive_min"):
        if key in d:
            setattr(c, key, float(d[key]))
    for key in ("unclear_cap_soc", "horizon_days", "run_every_min", "vehicle_limit_min", "retries"):
        if key in d:
            setattr(c, key, int(d[key]))
    c.rules = [Rule(str(r["match"]), Mode(str(r["mode"]).lower())) for r in (d.get("rules") or [])]
    if "dry_run" in d:
        c.dry_run = bool(d["dry_run"])
    web = d.get("web") or {}
    c.web_host = web.get("host", c.web_host)
    c.web_port = int(web.get("port", c.web_port))
    c.db_path = d.get("db_path") or c.db_path
    c.personal_dir = d.get("personal_dir") or ""
    c.timezone = d.get("timezone") or c.timezone
    return c


def load(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as fh:
        return from_dict(yaml.safe_load(fh) or {})


def problems(c: Config) -> list:
    """Check required settings; messages contain no secrets."""
    out = []
    if not c.ha_url:
        out.append("ha.url fehlt")
    if not c.ha_token:
        out.append("HA-Token fehlt (Umgebungsvariable aus ha.token_env)")
    if not c.evcc_url:
        out.append("evcc.url fehlt")
    if not c.evcc_key:
        out.append("evcc-API-Key fehlt (Umgebungsvariable aus evcc.api_key_env)")
    if not c.ors_key:
        out.append("ORS-Key fehlt (Umgebungsvariable aus ors.key_env)")
    if not c.home_known and not c.home_address:
        out.append("home.lat/lon oder home.address fehlt")
    return out
