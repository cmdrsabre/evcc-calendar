"""REST clients for Home Assistant, evcc and OpenRouteService (standard library only)."""
from __future__ import annotations

import json
import math
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Optional

from .models import Event, Place, Route


class ApiError(Exception):
    """Error from a service. The message contains neither URL parameters nor keys."""


class EvccDown(ApiError):
    """evcc does not respond even after several attempts."""


class OrsDown(ApiError):
    """OpenRouteService unreachable."""


def _request(method: str, url: str, headers: dict, body=None, timeout: float = 20.0):
    data = None
    hdrs = dict(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        hdrs["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
    host = urllib.parse.urlsplit(url).netloc
    path = urllib.parse.urlsplit(url).path
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
            status = resp.status
    except urllib.error.HTTPError as exc:
        raise ApiError("%s %s%s: HTTP %s" % (method, host, path, exc.code)) from None
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", exc)
        raise ApiError("%s %s%s: %s" % (method, host, path, reason)) from None
    if not raw:
        return status, None
    try:
        return status, json.loads(raw.decode("utf-8"))
    except ValueError:
        raise ApiError("%s %s%s: invalid JSON response" % (method, host, path)) from None


def _utc(text: str) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc)


# ---------------------------------------------------------------- Home Assistant
class HAClient:
    def __init__(self, url: str, token: str, timeout: float = 20.0):
        self.url, self.timeout = url.rstrip("/"), timeout
        self.headers = {"Authorization": "Bearer " + token}

    def calendar_events(self, entity: str, start: datetime, end: datetime) -> list:
        q = urllib.parse.urlencode({"start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                                    "end": end.strftime("%Y-%m-%dT%H:%M:%SZ")})
        _, data = _request("GET", "%s/api/calendars/%s?%s" % (self.url, entity, q), self.headers, timeout=self.timeout)
        out = []
        for raw in data or []:
            s, e = raw.get("start") or {}, raw.get("end") or {}
            if "dateTime" not in s or "dateTime" not in e:      # all-day -> ignore
                continue
            title = raw.get("summary") or ""
            start_dt, end_dt = _utc(s["dateTime"]), _utc(e["dateTime"])
            uid = raw.get("uid") or "%s|%s|%s" % (title, start_dt.isoformat(), raw.get("location") or "")
            out.append(Event(uid=uid, title=title, start=start_dt, end=end_dt,
                             location=(raw.get("location") or "").strip(),
                             description=raw.get("description") or ""))
        return out

    def notify(self, entities: list, title: str, message: str) -> None:
        """notify.send_message with target entities (app push, Telegram, Alexa announcement ...)."""
        _request("POST", "%s/api/services/notify/send_message" % self.url, self.headers,
                 {"entity_id": list(entities), "title": title, "message": message}, timeout=self.timeout)

    def weather_temperature(self, entity: str) -> Optional[float]:
        if not entity:
            return None
        _, data = _request("GET", "%s/api/states/%s" % (self.url, entity), self.headers, timeout=self.timeout)
        try:
            return float((data or {}).get("attributes", {}).get("temperature"))
        except (TypeError, ValueError):
            return None


# ---------------------------------------------------------------- evcc
class EvccClient:
    def __init__(self, url: str, api_key: str, timeout: float = 20.0):
        self.url, self.timeout = url.rstrip("/"), timeout
        self.headers = {"Authorization": "Bearer " + api_key} if api_key else {}

    def state(self) -> dict:
        _, data = _request("GET", self.url + "/api/state", self.headers, timeout=self.timeout)
        return (data or {}).get("result", data or {})

    @staticmethod
    def _vq(name: str) -> str:
        return urllib.parse.quote(name, safe="")

    def set_plan(self, vehicle: str, soc: int, when: datetime) -> None:
        ts = when.astimezone(timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ")
        _request("POST", "%s/api/vehicles/%s/plan/soc/%d/%s" % (
            self.url, self._vq(vehicle), soc, urllib.parse.quote(ts, safe="")), self.headers, timeout=self.timeout)

    def delete_plan(self, vehicle: str) -> None:
        _request("DELETE", "%s/api/vehicles/%s/plan/soc" % (self.url, self._vq(vehicle)),
                 self.headers, timeout=self.timeout)


def forecast_temperature(state: dict, at: datetime) -> Optional[float]:
    """Temperature from the evcc forecast (slots [start, end, value]) at time `at`."""
    slots = ((state.get("forecast") or {}).get("temperature")) or []
    best, best_d = None, None
    for s in slots:
        try:
            if isinstance(s, dict):
                a, b, v = s.get("start"), s.get("end"), s.get("value")
            else:
                a, b, v = s[0], s[1], s[2]
            a = _to_dt(a)
            b = _to_dt(b)
            if a <= at < b:
                return float(v)
            d = abs((a - at).total_seconds())
            if best_d is None or d < best_d:
                best, best_d = float(v), d
        except (TypeError, ValueError, IndexError):
            continue
    return best if best_d is not None and best_d < 3 * 3600 else None


def _to_dt(x) -> datetime:
    if isinstance(x, (int, float)):
        return datetime.fromtimestamp(x, tz=timezone.utc)
    return _utc(str(x))


# ---------------------------------------------------------------- OpenRouteService
ORS = "https://api.openrouteservice.org"


class OrsClient:
    def __init__(self, key: str, timeout: float = 20.0):
        self.key, self.timeout = key, timeout

    def geocode(self, text: str) -> Optional[Place]:
        q = urllib.parse.urlencode({"api_key": self.key, "text": text, "boundary.country": "DE", "size": 1})
        _, d = _request("GET", "%s/geocode/search?%s" % (ORS, q), {}, timeout=self.timeout)
        feats = (d or {}).get("features") or []
        if not feats:
            return None
        f = feats[0]
        p = f.get("properties") or {}
        lon, lat = f["geometry"]["coordinates"][:2]
        return Place(lat=lat, lon=lon, label=p.get("label") or "", confidence=float(p.get("confidence") or 0),
                     layer=p.get("layer") or "")

    def route(self, a: tuple, b: tuple) -> Route:
        """a, b = (lat, lon)."""
        q = urllib.parse.urlencode({"api_key": self.key, "start": "%s,%s" % (a[1], a[0]),
                                    "end": "%s,%s" % (b[1], b[0])})
        _, d = _request("GET", "%s/v2/directions/driving-car?%s" % (ORS, q), {}, timeout=self.timeout)
        try:
            sm = d["features"][0]["properties"]["summary"]
            return Route(distance_km=sm["distance"] / 1000.0, duration_min=sm["duration"] / 60.0)
        except (KeyError, IndexError, TypeError):
            raise ApiError("ORS directions: unexpected response") from None


def straight_line_route(a: tuple, b: tuple, factor: float = 1.3, speed_kmh: float = 60.0) -> Route:
    """Fallback when routing fails: straight-line distance x factor."""
    r = 6371.0
    la1, lo1, la2, lo2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((la2 - la1) / 2) ** 2 + math.cos(la1) * math.cos(la2) * math.sin((lo2 - lo1) / 2) ** 2
    km = 2 * r * math.asin(math.sqrt(h)) * factor
    return Route(distance_km=km, duration_min=km / speed_kmh * 60.0, estimated=True)
