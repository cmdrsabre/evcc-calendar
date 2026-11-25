from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import Optional

import yaml

from . import i18n
from .classify import Rule
from .live import DEFAULT_INTERVAL_S, clamp_interval
from .models import Mode

log = logging.getLogger("evccplan")


@dataclass
class Config:
    home_lat: float = 0.0
    home_lon: float = 0.0
    home_address: str = ""
    ha_url: str = ""
    ha_token: str = ""
    ha_calendars: list = field(default_factory=lambda: ["calendar.a_a"])
    ics_calendars: list = field(default_factory=list)    # (name, url) of ICS subscription feeds; the URL is a secret
    notify_targets: dict = field(default_factory=dict)   # level -> notify entities
    ha_weather: str = ""
    limit_entity: str = ""           # HA number entity of the car's charge limit (empty = feature off)
    limit_value: int = 100           # value the limit is raised to
    limit_auto: bool = True          # raise the limit automatically when it is below vehicle_limit_min
    evcc_url: str = ""
    evcc_key: str = ""
    evcc_ui_url: str = ""            # address of the evcc web interface as the browser sees it (default: evcc_url)
    evcc_vehicle: str = ""
    mqtt_enabled: bool = False       # MQTT link on/off (evcc as live source and/or publishing)
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_user: str = ""
    mqtt_password: str = ""          # only from the environment, never editable in the UI
    mqtt_topic: str = "evcc"         # evcc's topic prefix
    mqtt_read: bool = True           # take the live status from evcc's MQTT topics instead of polling
    mqtt_publish: bool = False       # publish the next plan
    mqtt_publish_topic: str = "evcc-calendar"
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
    charge_buffer_min: float = 3.0   # target reached this early, so small delays are not reported
    cold_buffer_min: float = 12.0    # extra when the temperature is below temp_threshold_c
    notify_min_late_min: float = 2.0  # lateness beyond the charge buffer (minutes) before a push is sent
    charge_power_kw: float = 11.0
    charge_loss_margin: float = 0.15
    chain_window_h: float = 5.0
    horizon_days: int = 6
    run_every_min: int = 30
    vehicle_limit_min: int = 100
    live_poll_s: int = DEFAULT_INTERVAL_S   # live status poll against evcc (seconds, 5 to 300)
    rules: list = field(default_factory=list)
    dry_run: bool = True
    web_host: str = "0.0.0.0"
    web_port: int = 80
    db_path: str = "/data/state.db"
    personal_dir: str = ""           # personal images (not in the repo); default: "personal" folder next to the database
    timezone: str = "Europe/Berlin"
    language: str = "en"             # UI and message language (see evccplan/static/locales)
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
    ics = d.get("ics") or []
    cal = ha.get("calendar") or ([] if ics else c.ha_calendars)
    c.ha_calendars = [cal] if isinstance(cal, str) else [str(x) for x in cal]     # one entity or a list
    for i, entry in enumerate(ics):
        name, url = str((entry or {}).get("name") or ""), env((entry or {}).get("url_env"))
        if not name or not url:
            log.warning("ics entry %d ignored: name or URL (environment variable from url_env) is missing", i + 1)
        else:
            c.ics_calendars.append((name, url))
    tg = ha.get("notify_targets") or {}
    for lvl in ("info", "warning", "error"):
        v = tg.get(lvl) or []
        c.notify_targets[lvl] = [v] if isinstance(v, str) else list(v)
    c.ha_weather = ha.get("weather") or ""
    cl = ha.get("charge_limit") or {}
    ent = str(cl.get("entity") or "")
    if ent and not ent.startswith(("number.", "input_number.")):
        log.warning("ha.charge_limit.entity must be a number or input_number entity, ignoring %r", ent)
        ent = ""
    c.limit_entity = ent
    c.limit_value = min(max(int(cl.get("value", c.limit_value)), 50), 100)
    c.limit_auto = bool(cl.get("auto", c.limit_auto))
    ev = d.get("evcc") or {}
    c.evcc_url = (ev.get("url") or "").rstrip("/")
    c.evcc_key = env(ev.get("api_key_env"))
    ui = str(ev.get("ui_url") or "").rstrip("/")
    c.evcc_ui_url = ui if ui.startswith(("http://", "https://")) else c.evcc_url
    c.evcc_vehicle = ev.get("vehicle") or ""
    mq = ev.get("mqtt") or {}
    c.mqtt_enabled = bool(mq.get("enabled", c.mqtt_enabled))
    c.mqtt_host = str(mq.get("host") or "").strip()
    c.mqtt_port = int(mq.get("port", c.mqtt_port))
    c.mqtt_user = str(mq.get("user") or "")
    c.mqtt_password = env(mq.get("password_env") or "EVCC_MQTT_PASSWORD")   # works without an evcc.mqtt block (UI only)
    c.mqtt_topic = str(mq.get("topic") or c.mqtt_topic).strip("/")
    c.mqtt_read = bool(mq.get("read_evcc", c.mqtt_read))
    c.mqtt_publish = bool(mq.get("publish", c.mqtt_publish))
    c.mqtt_publish_topic = str(mq.get("publish_topic") or c.mqtt_publish_topic).strip("/")
    if c.mqtt_enabled and not c.mqtt_host:
        log.warning("evcc.mqtt.enabled is set but evcc.mqtt.host is empty, MQTT stays off")
    ors = d.get("ors") or {}
    c.ors_key = env(ors.get("key_env"))
    c.ors_min_confidence = float(ors.get("min_confidence", c.ors_min_confidence))
    cons = d.get("consumption") or {}
    c.warm = float(cons.get("warm", c.warm))
    c.cold = float(cons.get("cold", c.cold))
    c.temp_threshold_c = float(cons.get("temp_threshold_c", c.temp_threshold_c))
    for key in ("reserve_soc", "time_buffer_min", "charge_buffer_min", "cold_buffer_min", "charge_power_kw",
                "charge_loss_margin", "chain_window_h", "retry_delay_s", "min_car_km", "manual_drive_min",
                "notify_min_late_min"):
        if key in d:
            setattr(c, key, float(d[key]))
    for key in ("unclear_cap_soc", "horizon_days", "run_every_min", "vehicle_limit_min", "retries"):
        if key in d:
            setattr(c, key, int(d[key]))
    if "live_poll_s" in d:
        c.live_poll_s = clamp_interval(d["live_poll_s"])
    c.rules = [Rule(str(r["match"]), Mode(str(r["mode"]).lower())) for r in (d.get("rules") or [])]
    if "dry_run" in d:
        c.dry_run = bool(d["dry_run"])
    web = d.get("web") or {}
    c.web_host = web.get("host", c.web_host)
    c.web_port = int(web.get("port", c.web_port))
    c.db_path = d.get("db_path") or c.db_path
    c.personal_dir = d.get("personal_dir") or ""
    c.timezone = d.get("timezone") or c.timezone
    lang = str(d.get("language") or c.language).lower()
    c.language = lang if lang in i18n.available() else c.language
    return c


def load(path: str) -> Config:
    with open(path, "r", encoding="utf-8") as fh:
        return from_dict(yaml.safe_load(fh) or {})


def problems(c: Config) -> list:
    """Check required settings; messages contain no secrets."""
    out = []
    if not c.ha_url:
        out.append("ha.url is missing")
    if not c.ha_token:
        out.append("HA token is missing (environment variable from ha.token_env)")
    if not c.ha_calendars and not c.ics_calendars:
        out.append("no calendar configured (ha.calendar or ics)")
    names = [n for n, _ in c.ics_calendars]
    if len(set(names)) != len(names):
        out.append("ics names must be unique")
    if not c.evcc_url:
        out.append("evcc.url is missing")
    if not c.evcc_key:
        out.append("evcc API key is missing (environment variable from evcc.api_key_env)")
    if not c.ors_key:
        out.append("ORS key is missing (environment variable from ors.key_env)")
    if not c.home_known and not c.home_address:
        out.append("home.lat/lon or home.address is missing")
    return out
