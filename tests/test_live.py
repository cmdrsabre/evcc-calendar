"""Live status: state parser, poller (unreachable, intervals), isolation from planning runs, /api/live."""
import json
import threading
import time
from pathlib import Path

import pytest

from evccplan import live as livemod
from evccplan.clients import ApiError, EvccClient
from evccplan.config import Config, from_dict
from evccplan.live import LivePoller, clamp_interval, parse_state
from evccplan.settings import validate

from test_e2e import env  # noqa: F401  (fixture)
from test_web import Client, authmod, setup_login, web  # noqa: F401

FIX = Path(__file__).resolve().parent / "fixtures"


def fixture(name):
    return json.loads((FIX / name).read_text(encoding="utf-8"))


class FakeEvcc:
    def __init__(self, state=None, error=None):
        self.next, self.error, self.calls = state, error, 0

    def state(self):
        self.calls += 1
        if self.error:
            raise self.error
        return self.next


# ------------------------------------------------------------------ parser
def test_parse_state_with_pv_and_grid_values():
    out = parse_state(fixture("evcc_state_pv.json"), "db:1")
    assert out["loadpoint"] == {"connected": True, "charging": True, "power_kw": 6.42, "soc": 63.4, "energy_kwh": 12.35}
    assert out["site"] == {"pv_kw": 4.21, "grid_kw": -2.55, "home_kw": 0.83}      # grid < 0: feeding in


def test_parse_state_without_pv_data_leaves_the_site_part_out():
    out = parse_state(fixture("evcc_state_nopv.json"), "db:1")
    assert out["site"] == {}
    assert out["loadpoint"]["charging"] is False
    assert out["loadpoint"]["power_kw"] == 0.0 and out["loadpoint"]["soc"] == 67.0    # real zeros stay


def test_parse_state_is_defensive():
    assert parse_state(None) == {"loadpoint": {}, "site": {}}
    assert parse_state([1, 2]) == {"loadpoint": {}, "site": {}}
    bad = {"pvPower": "4210", "homePower": True, "grid": "x", "gridPower": None,
           "loadpoints": [None, "x", {"vehicleName": "db:1", "connected": "yes", "charging": 1,
                                       "chargePower": None, "chargedEnergy": float("nan"), "vehicleSoc": [60]}]}
    assert parse_state(bad, "db:1") == {"loadpoint": {}, "site": {}}


def test_parse_state_grid_fallback_and_loadpoint_choice():
    assert parse_state({"gridPower": 1500}, "")["site"] == {"grid_kw": 1.5}
    two = {"loadpoints": [{"vehicleName": "a", "vehicleSoc": 10}, {"vehicleName": "b", "vehicleSoc": 20}]}
    assert parse_state(two, "b")["loadpoint"] == {"soc": 20.0}
    assert parse_state(two, "zzz")["loadpoint"] == {}                  # several loadpoints, none matches: show nothing
    assert parse_state({"loadpoints": [{"vehicleName": "", "vehicleSoc": 55}]}, "db:1")["loadpoint"] == {"soc": 55.0}


# ------------------------------------------------------------------ poller
def test_poller_caches_and_reports_unreachable_without_crashing():
    cfg = Config(evcc_vehicle="db:1")
    fake = FakeEvcc(fixture("evcc_state_pv.json"))
    p = LivePoller(fake, cfg)
    assert p.snapshot()["reachable"] is None                      # nothing polled yet
    p.poll_once()
    snap = p.snapshot()
    assert snap["reachable"] is True and snap["loadpoint"]["power_kw"] == 6.42 and snap["ok_time"]
    ok_time = snap["ok_time"]
    p.snapshot(), p.snapshot()
    assert fake.calls == 1                                        # reading the cache does not call evcc

    fake.error = ApiError("GET evcc/api/state: timed out")
    p.poll_once()
    down = p.snapshot()
    assert down["reachable"] is False and down["loadpoint"] == {} and down["site"] == {}
    assert down["ok_time"] == ok_time                             # last success is remembered

    fake.error = RuntimeError("unexpected")                       # not an ApiError: still no crash
    p.poll_once()
    assert p.snapshot()["reachable"] is False

    fake.error = None
    p.poll_once()
    assert p.snapshot()["reachable"] is True


def test_poller_with_a_closed_port_is_unreachable():
    p = LivePoller(EvccClient("http://127.0.0.1:9", "k", timeout=1), Config())
    p.poll_once()
    assert p.snapshot()["reachable"] is False


def test_poller_thread_polls_repeatedly_and_stops(monkeypatch):
    cfg = Config(evcc_vehicle="db:1")
    monkeypatch.setattr(LivePoller, "interval_s", property(lambda self: 0.05))      # the real limit is 5 s
    fake = FakeEvcc(fixture("evcc_state_pv.json"))
    p = LivePoller(fake, cfg)
    p.start()
    end = time.time() + 5
    while fake.calls < 3 and time.time() < end:
        time.sleep(0.02)
    p.stop()
    assert fake.calls >= 3


# ------------------------------------------------------------------ intervals
def test_interval_limits():
    assert [clamp_interval(v) for v in (1, 4, 5, 10, 300, 301, 9999)] == [5, 5, 5, 10, 300, 300, 300]
    assert clamp_interval(7.4) == 7
    for bad in (None, "10", True, float("nan"), float("inf")):
        assert clamp_interval(bad) == livemod.DEFAULT_INTERVAL_S


def test_interval_in_config_settings_and_poller():
    assert Config().live_poll_s == 10
    assert from_dict({"live_poll_s": 1}).live_poll_s == 5
    assert from_dict({"live_poll_s": 5000}).live_poll_s == 300
    assert from_dict({}).live_poll_s == 10
    assert validate({"live_poll_s": 5}) == {"live_poll_s": 5} and validate({"live_poll_s": 300}) == {"live_poll_s": 300}
    for bad in (4, 301, 0, -1, 7.5, "10", True):
        with pytest.raises(ValueError):
            validate({"live_poll_s": bad})
    cfg = Config()
    p = LivePoller(FakeEvcc({}), cfg)
    cfg.live_poll_s = 30
    assert p.interval_s == 30 and p.snapshot()["interval_s"] == 30
    cfg.live_poll_s = 1                                           # damaged value: still within the limits
    assert p.interval_s == 5


# ------------------------------------------------------------------ isolation from planning runs
def test_poll_reads_only_evcc_state_and_never_starts_a_run(env):
    srv, cfg, runner, state, ors, store = env
    runs = []
    runner.run = lambda *a, **k: runs.append(1)                   # any planning run would show up here
    srv.state.update(fixture("evcc_state_pv.json"))
    p = LivePoller(runner.evcc, cfg)
    for _ in range(3):
        p.poll_once()
    assert p.snapshot()["reachable"] is True
    assert set(srv.gets) == {"/api/state"} and len(srv.gets) == 3     # no calendar, no weather, nothing else
    assert runs == [] and store.runs(10) == []
    assert ors.geocodes == [] and ors.routes == [] and srv.calls == [] and srv.notified == []


def test_concurrent_triggers_never_run_in_parallel_and_the_poll_does_not_wait(env):
    srv, cfg, runner, state, ors, store = env
    real = runner.evcc

    class Gate:
        """Holds the first run inside evcc.state() so that further triggers meet the lock."""
        def __init__(self):
            self.entered, self.release = threading.Event(), threading.Event()
            self.active = self.max_active = self.calls = 0
            self._m = threading.Lock()

        def state(self):
            with self._m:
                self.calls += 1
                self.active += 1
                self.max_active = max(self.max_active, self.active)
            self.entered.set()
            self.release.wait(10)
            try:
                return real.state()
            finally:
                with self._m:
                    self.active -= 1

        def __getattr__(self, name):
            return getattr(real, name)

    gate = runner.evcc = Gate()
    first = threading.Thread(target=runner.run)
    first.start()
    assert gate.entered.wait(5)
    assert runner.busy()
    second = threading.Thread(target=runner.run)                  # e.g. timer or UI trigger during a run
    second.start()
    time.sleep(0.3)
    assert gate.calls == 1 and gate.active == 1                   # the second run has not started

    poller = LivePoller(real, cfg)                                # the status poll does not use the run lock
    started = time.time()
    poller.poll_once()
    assert time.time() - started < 5 and poller.snapshot()["reachable"] is True
    assert gate.calls == 1

    gate.release.set()
    first.join(20), second.join(20)
    assert not first.is_alive() and not second.is_alive()
    assert gate.max_active == 1 and gate.calls == 2               # strictly one after the other
    assert not runner.busy()


# ------------------------------------------------------------------ /api/live
def test_api_live_serves_the_cache_only(web):
    c, runner, srv, store, httpd = web
    assert c.call("GET", "/api/live")[0] == 401
    setup_login(c)
    code, first, hdrs = c.call("GET", "/api/live")
    assert code == 200 and first["reachable"] is None and first["interval_s"] == 10
    assert hdrs["Cache-Control"] == "no-store"
    srv.state.update(fixture("evcc_state_pv.json"))
    httpd.live.poll_once()
    n = len(srv.gets)
    for _ in range(3):
        code, data, _ = c.call("GET", "/api/live")
    assert code == 200 and data["reachable"] is True
    assert data["site"]["pv_kw"] == 4.21 and data["loadpoint"]["charging"] is True
    assert len(srv.gets) == n                                     # requests from the UI never reach evcc


def test_live_poll_interval_is_editable_in_settings(web):
    c, runner, srv, store, httpd = web
    setup_login(c)
    code, data, _ = c.call("POST", "/api/settings", {"values": {"live_poll_s": 30}})
    assert code == 200 and data["values"]["live_poll_s"] == 30
    assert c.call("GET", "/api/live")[1]["interval_s"] == 30
    assert c.call("POST", "/api/settings", {"values": {"live_poll_s": 3}})[0] == 400


def test_poller_snapshot_carries_the_charge_view():
    from datetime import datetime, timedelta, timezone
    now = datetime(2026, 10, 14, 22, 0, tzinfo=timezone.utc)
    st = {"vehicles": {"db:1": {"capacity": 54, "plan": {"soc": 85, "time": (now + timedelta(hours=9)).strftime("%Y-%m-%dT%H:%M:%SZ")}}},
          "loadpoints": [{"vehicleName": "db:1", "connected": False}], "tariffGrid": 0.3, "currency": "EUR"}
    cfg = Config(evcc_vehicle="db:1")
    poller = LivePoller(FakeEvcc(st), cfg, lambda: now)
    assert poller.snapshot()["charge"] is None                          # nothing polled yet
    poller.poll_once()
    c = poller.snapshot()["charge"]
    assert c["state"] == "away" and c["cost"]["max"] == 14.29
    st["vehicles"]["db:1"]["capacity"] = "odd"                           # unreadable data: the view goes, the status stays
    poller.poll_once()
    snap = poller.snapshot()
    assert snap["reachable"] is True and snap["charge"] is None


def test_poller_backs_off_while_evcc_fails():
    poller = LivePoller(FakeEvcc(), Config(live_poll_s=10), lambda: __import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    assert [poller.next_wait_s(n) for n in (0, 1, 2, 3, 6, 40)] == [10, 20, 40, 80, 300, 300]
