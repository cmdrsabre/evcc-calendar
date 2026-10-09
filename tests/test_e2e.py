"""Ende-zu-Ende mit den echten Terminen und Werten aus dem POC-Lauf (Fake-Server fuer HA und evcc)."""
import json
import threading
import urllib.parse
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from evccplan.classify import Rule
from evccplan.clients import ApiError, EvccClient, HAClient, forecast_temperature
from evccplan.config import Config, from_dict, problems
from evccplan.models import Mode, Place, Route
from evccplan.runner import Runner
from evccplan.store import Store

UTC = timezone.utc
TARGETS = {"info": ["notify.a"], "warning": ["notify.a", "notify.b"], "error": ["notify.a", "notify.b", "notify.c"]}

CAL = [  # (Titel, Start, Ende, Ort) - lokale Zeit MESZ, aus dem POC
    ("Kinder bei Omi (Zeit noch nicht fest)", "2026-10-11T14:00:00+02:00", "2026-10-11T17:00:00+02:00", ""),
    ("Termin - MEG-Radiologie Ludwigsfelde", "2026-10-12T08:15:00+02:00", "2026-10-12T08:30:00+02:00",
     "Albert-Schweitzer-Straße 40, 14974 Ludwigsfelde"),
    ("Auswertungsgespräch Arthur", "2026-10-12T11:55:00+02:00", "2026-10-12T12:25:00+02:00",
     "Isabelle Plessow Kinder- und Jugendlichenpsychotherapeutin Roedernstraße 15, 12459 Berlin"),
    ("Hausarzt - Juliana Guerrero", "2026-10-12T16:00:00+02:00", "2026-10-12T16:30:00+02:00",
     "Smart Care MVZ Ludwigsfelde - Dr. Juliana Guerrero Straße der Jugend 63 14974 Ludwigsfelde"),
    ("Axel bei Fritz!", "2026-10-13T09:00:00+02:00", "2026-10-13T17:00:00+02:00", "Stromstraße 7\n10555 Berlin\nDeutschland"),
    ("Termin - Charité | Hals-, Nasen- und Ohrenheilkunde (HNO)", "2026-10-13T10:40:00+02:00",
     "2026-10-13T11:00:00+02:00", "Mittelallee 2, 13353 Berlin"),
    ("Hooptraining", "2026-10-13T18:00:00+02:00", "2026-10-13T19:00:00+02:00",
     "Volkshochschule Treptow-Köpenick, Baumschulenstraße 79-81, 12437 Berlin, Deutschland"),
    ("Anne Uni", "2026-10-14T09:45:00+02:00", "2026-10-14T11:00:00+02:00",
     "Hochschule für Technik und Wirtschaft Berlin (HTW Berlin) - Campus Wilhelminenhof, "
     "Wilhelminenhofstraße 75A, 12459 Berlin-Bezirk Treptow-Köpenick, Deutschland"),
    ("Termin - MEG-Radiologie Ludwigsfelde", "2026-10-16T10:30:00+02:00", "2026-10-16T11:00:00+02:00",
     "Albert-Schweitzer-Straße 40, 14974 Ludwigsfelde"),
]
KM = {"Albert-Schweitzer-Straße 40, 14974 Ludwigsfelde": (2.1, 8), "Roedernstraße 15, 12459 Berlin": (42.9, 45),
      "Straße der Jugend 63, 14974 Ludwigsfelde": (0.5, 2), "Stromstraße 7, 10555 Berlin": (45.1, 52),
      "Mittelallee 2, 13353 Berlin": (47.4, 52), "Baumschulenstraße 79-81, 12437 Berlin": (44.2, 41),
      "Wilhelminenhofstraße 75A, 12459 Berlin": (44.0, 41)}


class FakeOrs:
    def __init__(self):
        self.geocodes, self.routes = [], []

    def geocode(self, text):
        self.geocodes.append(text)
        if text in KM:
            return Place(52.0 + len(self.geocodes) / 1000, 13.0, text, 1.0, "address")
        if text.startswith("Smart Care") or text == "Ludwigsfelde":
            return Place(52.3, 13.2, "Ludwigsfelde, BB, Germany", 0.6, "locality")
        return None

    def route(self, a, b):
        self.routes.append((a, b))
        raise AssertionError("route() wird ueber die KM-Tabelle ersetzt")


class TableOrs(FakeOrs):
    """Liefert Strecken aus der KM-Tabelle, anhand der zuletzt geocodierten Adresse."""
    def __init__(self):
        super().__init__()
        self.by_coord = {}

    def geocode(self, text):
        p = super().geocode(text)
        if p and text in KM:
            self.by_coord[(p.lat, p.lon)] = KM[text]
        return p

    def route(self, a, b):
        km, mn = self.by_coord[b]
        return Route(km, mn)


class Server:
    def __init__(self, state, calendar):
        self.state, self.calendar = state, calendar
        self.calls, self.notified, self.targets, self.fail_calendar = [], [], [], False
        outer = self

        class H(BaseHTTPRequestHandler):
            def _auth(self):
                return self.headers.get("Authorization") == "Bearer tok"

            def _reply(self, code, obj=None):
                data = b"" if obj is None else json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if not self._auth():
                    return self._reply(401)
                u = urllib.parse.urlsplit(self.path)
                if u.path == "/api/state":
                    return self._reply(200, {"result": outer.state})
                if u.path.startswith("/api/calendars/"):
                    if outer.fail_calendar:
                        return self._reply(500)
                    q = urllib.parse.parse_qs(u.query)
                    s = datetime.fromisoformat(q["start"][0].replace("Z", "+00:00"))
                    e = datetime.fromisoformat(q["end"][0].replace("Z", "+00:00"))
                    out = []
                    for title, a, b, loc in outer.calendar:
                        if s <= datetime.fromisoformat(a) <= e:
                            out.append({"summary": title, "start": {"dateTime": a}, "end": {"dateTime": b},
                                        "location": loc})
                    out.append({"summary": "Ganztag", "start": {"date": "2026-10-12"}, "end": {"date": "2026-10-13"}})
                    return self._reply(200, out)
                if u.path.startswith("/api/states/weather."):
                    return self._reply(200, {"attributes": {"temperature": 11.8}})
                self._reply(404)

            def _write(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n)) if n else None
                if not self._auth():
                    return self._reply(401)
                if self.path == "/api/services/notify/send_message":
                    outer.notified.append(body["message"])
                    outer.targets.append(body["entity_id"])
                else:
                    outer.calls.append((self.command, self.path))
                self._reply(200, {})

            do_POST = do_DELETE = _write

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), H)
        self.url = "http://127.0.0.1:%d" % self.httpd.server_port
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()


def evcc_state(plan=None, **lp):
    slot = [int(datetime(2026, 10, 9, tzinfo=UTC).timestamp()), int(datetime(2026, 10, 20, tzinfo=UTC).timestamp()), 12.0]
    return {"vehicles": {"db:1": {"title": "Ioniq 5", "capacity": 54, "minSoc": 30, "limitSoc": 80,
                                  "plan": plan, "repeatingPlans": [
                                      {"active": True, "time": "17:00", "soc": 60, "tz": "Europe/Berlin", "weekdays": [2]}]}},
            "loadpoints": [dict({"vehicleName": "db:1", "vehicleSoc": 67.1, "vehicleLimitSoc": 100, "connected": True,
                                 "mode": "smart", "effectivePlanId": 0}, **lp)],
            "forecast": {"temperature": [slot]}, "tariffTemperature": 12}


@pytest.fixture
def env():
    srv = Server(evcc_state(), CAL)
    cfg = Config(ha_url=srv.url, ha_token="tok", ha_calendar="calendar.a_a", notify_targets=TARGETS,
                 evcc_url=srv.url, evcc_key="tok", evcc_vehicle="db:1", ors_key="k", home_lat=52.3, home_lon=13.2,
                 rules=[Rule("fritz*", Mode.BAHN), Rule("uni*", Mode.AUTO)], dry_run=False, retry_delay_s=0)
    ors = TableOrs()
    store = Store(":memory:")
    state = {"now": datetime(2026, 10, 12, 6, 0, tzinfo=UTC)}      # nach Abfahrt zum MEG-Termin
    runner = Runner(cfg, store, HAClient(srv.url, "tok"), EvccClient(srv.url, "tok"), ors, lambda: state["now"])
    yield srv, cfg, runner, state, ors, store
    srv.close()


def status_by_title(res):
    return {i["title"].split(" (")[0] + "@" + i["start"][:10]: i["status"] for i in res["items"]}


def test_real_week_live_run_sets_expected_plan(env):
    srv, cfg, runner, state, ors, store = env
    res = runner.run()
    assert res["ok"], res["error"]
    st = status_by_title(res)
    assert not any(k.startswith("Kinder bei Omi") for k in st)            # liegt vor "jetzt"
    assert st["Axel bei Fritz!@2026-10-13"] == "bahn"
    assert st["Termin - MEG-Radiologie Ludwigsfelde@2026-10-12"] == "nah"
    assert st["Auswertungsgespräch Arthur@2026-10-12"] == "geplant"
    d = res["desired"]
    assert d["soc"] == 45 and d["time"] == "2026-10-12T08:40:00Z"
    assert srv.calls == [("POST", "/api/vehicles/db%3A1/plan/soc/45/2026-10-12T08%3A40%3A00Z")]
    assert store.get("last_set") == {"soc": 45, "time": "2026-10-12T08:40:00Z"}
    # die MVZ-Adresse wurde ueber den Vorfilter aufgeloest, nicht ueber den Ortskern
    assert "Straße der Jugend 63, 14974 Ludwigsfelde" in ors.geocodes
    assert st["Anne Uni@2026-10-14"] in ("verkettet", "spaeter")


def test_second_run_is_idempotent(env):
    srv, cfg, runner, state, ors, store = env
    runner.run()
    srv.state["vehicles"]["db:1"]["plan"] = {"soc": 45, "time": "2026-10-12T08:40:00Z"}
    n_geo = len(ors.geocodes)
    res = runner.run()
    assert res["action"]["kind"] == "none"
    assert len(srv.calls) == 1
    assert len(ors.geocodes) == n_geo                                     # Geocoding kommt aus dem Cache


def test_dry_run_writes_nothing_and_sends_nothing(env):
    srv, cfg, runner, state, ors, store = env
    cfg.dry_run = True
    res = runner.run()
    assert res["dry_run"] and res["action"]["kind"] == "set" and not res["action"]["done"]
    assert srv.calls == [] and srv.notified == [] and store.get("last_set") is None


def test_manual_plan_untouched_and_single_notification(env):
    srv, cfg, runner, state, ors, store = env
    srv.state["vehicles"]["db:1"]["plan"] = {"soc": 90, "time": "2026-10-14T05:00:00Z"}
    r1 = runner.run()
    assert r1["action"]["kind"] == "manual" and srv.calls == []
    assert any("manuell" in m for m in srv.notified)
    n = len(srv.notified)
    runner.run()
    assert len(srv.notified) == n                                        # nur einmal melden


def test_service_deletes_only_its_own_plan(env):
    srv, cfg, runner, state, ors, store = env
    runner.run()                                                          # setzt 45 %
    srv.state["vehicles"]["db:1"]["plan"] = {"soc": 45, "time": "2026-10-12T08:40:00Z"}
    srv.calendar = [c for c in CAL if "Roedernstraße" not in c[3] and "Straße der Jugend" not in c[3]]
    state["now"] = datetime(2026, 10, 12, 7, 0, tzinfo=UTC)
    srv.calls.clear()
    # Charité-Termin (Di) ist der naechste; Plan "45 %" gehoert dem Dienst, wird ersetzt statt geloescht
    res = runner.run()
    assert res["action"]["kind"] == "set"
    # Kalender leer -> eigener Plan wird entfernt
    srv.calendar = []
    srv.state["vehicles"]["db:1"]["plan"] = {"soc": res["desired"]["soc"], "time": res["desired"]["time"]}
    srv.calls.clear()
    res = runner.run()
    assert res["action"]["kind"] == "delete" and srv.calls[0][0] == "DELETE"


def test_before_first_departure_no_plan_needed(env):
    srv, cfg, runner, state, ors, store = env
    state["now"] = datetime(2026, 10, 9, 10, 0, tzinfo=UTC)
    res = runner.run()
    assert status_by_title(res)["Termin - MEG-Radiologie Ludwigsfelde@2026-10-12"] == "nah"
    assert res["desired"]["soc"] == 45 and res["desired"]["title"].startswith("Auswertungsgespräch")
    assert len(res["items"]) == 8                       # Termin am 16.10. liegt ausserhalb der 6 Tage
    assert all(i["start"] < "2026-10-16" for i in res["items"])


def test_car_limit_warning_sent(env):
    srv, cfg, runner, state, ors, store = env
    srv.state["loadpoints"][0]["vehicleLimitSoc"] = 80
    runner.run()
    assert any("Ladelimit im Auto" in m for m in srv.notified)


def test_unreachable_calendar_sends_outage_once_and_keeps_running(env):
    srv, cfg, runner, state, ors, store = env
    srv.fail_calendar = True
    r = runner.run()
    assert not r["ok"] and "HTTP 500" in r["error"]
    runner.run()
    assert len([m for m in srv.notified if "gestört" in m]) == 1


def test_wrong_token_does_not_leak_secret(env):
    srv, cfg, runner, state, ors, store = env
    bad = HAClient(srv.url, "geheim123")
    with pytest.raises(ApiError) as exc:
        bad.calendar_events("calendar.a_a", state["now"], state["now"])
    assert "geheim123" not in str(exc.value) and "401" in str(exc.value)


def test_forecast_temperature_slots():
    st = evcc_state()
    assert forecast_temperature(st, datetime(2026, 10, 12, 8, 0, tzinfo=UTC)) == 12.0
    assert forecast_temperature(st, datetime(2027, 1, 1, tzinfo=UTC)) is None


def test_config_from_dict_and_problems():
    c = from_dict({"ha": {"url": "http://x:8123/", "token_env": "T", "calendar": "calendar.a_a"},
                   "evcc": {"url": "http://y:7070", "api_key_env": "K", "vehicle": "db:1"},
                   "rules": [{"match": "FRITZ*", "mode": "bahn"}], "horizon_days": 6},
                  env=lambda n: {"T": "t", "K": "k"}.get(n, ""))
    assert c.ha_url == "http://x:8123" and c.ha_token == "t" and c.evcc_key == "k"
    assert c.rules[0].mode == Mode.BAHN and c.dry_run is True
    assert any("ORS" in p for p in problems(c))


def test_ors_outage_is_collected_not_spammed_and_run_continues(env):
    srv, cfg, runner, state, ors, store = env

    def boom(text):
        raise ApiError("GET api.openrouteservice.org/geocode/search: timed out")
    ors.geocode = boom
    r1 = runner.run()
    runner.run()
    assert r1["ok"] and any("OpenRouteService" in p for p in r1["problems"])
    assert srv.calls == []
    assert len([m for m in srv.notified if "OpenRouteService" in m]) == 1
    assert not any("Adresse unklar" in m for m in srv.notified)


class DeadEvcc(EvccClient):
    attempts = 0

    def state(self):
        DeadEvcc.attempts += 1
        raise ApiError("GET 192.168.178.10:7070/api/state: Connection refused")


def test_evcc_down_three_attempts_then_alarm_once(env):
    srv, cfg, runner, state, ors, store = env
    runner.run()                                                       # guter Lauf, Stand merken
    runner.evcc = DeadEvcc("http://x", "tok")
    DeadEvcc.attempts = 0
    r = runner.run()
    assert DeadEvcc.attempts == 3
    assert not r["ok"] and "nach 3 Versuchen" in r["error"]
    assert r["items"] and r["stale_since"]                              # letzter guter Stand bleibt sichtbar
    runner.run()
    assert len([m for m in srv.notified if m.startswith("ALARM: evcc")]) == 1


def test_calendar_down_uses_last_known_events_and_warns(env):
    srv, cfg, runner, state, ors, store = env
    runner.run()                                                       # setzt Plan, merkt Termine
    srv.state["vehicles"]["db:1"]["plan"] = None                       # Plan ist weg -> muss neu gesetzt werden
    srv.fail_calendar = True
    srv.calls.clear()
    r = runner.run()
    assert r["ok"] and r["desired"]["soc"] == 45
    assert r["stale_since"] and any("Kalender nicht lesbar" in p for p in r["problems"])
    assert srv.calls and srv.calls[0][0] == "POST"                      # evcc wird weiter bedient
    runner.run()
    assert len([m for m in srv.notified if "Kalender nicht lesbar" in m]) == 1


def test_home_assistant_completely_down_still_serves_evcc(env):
    srv, cfg, runner, state, ors, store = env
    runner.run()
    srv.state["vehicles"]["db:1"]["plan"] = None
    srv.fail_calendar = True

    def no_push(*a, **k):
        raise ApiError("POST 127.0.0.1/api/services/notify/mobile_app_x: HTTP 503")
    runner.ha.notify = no_push
    srv.calls.clear()
    r = runner.run()
    assert r["ok"] and srv.calls and srv.calls[0][0] == "POST"
    assert any("Push über Home Assistant nicht möglich" in p for p in r["problems"])
    assert any(n["status"].startswith("Senden fehlgeschlagen") for n in r["notices"])


def test_cold_start_with_calendar_down_and_no_cache_reports_error(env):
    srv, cfg, runner, state, ors, store = env
    srv.fail_calendar = True
    r = runner.run()
    assert not r["ok"] and "Home Assistant" in r["error"] and srv.calls == []


def test_notification_targets_by_level(env):
    srv, cfg, runner, state, ors, store = env
    runner.run()                                                     # Plan gesetzt -> Bestaetigung
    i = [k for k, m in enumerate(srv.notified) if m.startswith("Ladeplan gesetzt")]
    assert len(i) == 1 and srv.targets[i[0]] == ["notify.a"]
    srv.state["loadpoints"][0]["vehicleLimitSoc"] = 80              # Warnung
    runner.run()
    k = [k for k, m in enumerate(srv.notified) if "Ladelimit im Auto" in m][0]
    assert srv.targets[k] == ["notify.a", "notify.b"]
    srv.fail_calendar = True                                         # Fehler
    runner.run()
    k = [k for k, m in enumerate(srv.notified) if "Kalender nicht lesbar" in m][0]
    assert srv.targets[k] == ["notify.a", "notify.b", "notify.c"]


def test_level_without_targets_is_reported_not_crashing(env):
    srv, cfg, runner, state, ors, store = env
    cfg.notify_targets = {"info": [], "warning": [], "error": []}
    r = runner.run()
    assert r["ok"] and srv.notified == []
    assert any("kein Ziel" in n["status"] for n in r["notices"])


def test_config_notify_targets_parse():
    c = from_dict({"ha": {"notify_targets": {"info": "notify.x", "error": ["notify.x", "notify.y"]}}})
    assert c.notify_targets == {"info": ["notify.x"], "warning": [], "error": ["notify.x", "notify.y"]}
