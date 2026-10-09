"""Web API: setup, login, per-event overrides, security rules."""
import json
import time
import urllib.error
import urllib.request

import pytest

from evccplan import auth as authmod
from evccplan.web import make_server

from test_e2e import env, status_by_title  # noqa: F401  (reuse fixture and helpers)

authmod.ITERATIONS = 1000        # fast tests


class Client:
    def __init__(self, base):
        self.base, self.cookie = base, None

    def call(self, method, path, body=None, csrf=True, headers=None, ctype="application/json"):
        data = None if body is None else json.dumps(body).encode()
        h = {"Content-Type": ctype} if data is not None else {}
        if csrf and method == "POST":
            h["X-CSRF"] = "1"
        if self.cookie:
            h["Cookie"] = self.cookie
        h.update(headers or {})
        req = urllib.request.Request(self.base + path, data=data, method=method, headers=h)
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                code, raw, hdrs = r.status, r.read(), r.headers
        except urllib.error.HTTPError as e:
            code, raw, hdrs = e.code, e.read(), e.headers
        sc = hdrs.get("Set-Cookie")
        if sc:
            self.cookie = sc.split(";")[0] if not sc.split(";")[0].endswith("=") else None
        try:
            return code, json.loads(raw), hdrs
        except ValueError:
            return code, raw, hdrs


@pytest.fixture
def web(env):
    srv, cfg, runner, state, ors, store = env
    cfg.web_host, cfg.web_port, cfg.dry_run = "127.0.0.1", 0, False
    httpd = make_server(runner, cfg)
    import threading
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    runner.run()
    c = Client("http://127.0.0.1:%d" % httpd.server_address[1])
    yield c, runner, srv, store, httpd
    httpd.shutdown()


def setup_login(c):
    code, _, _ = c.call("POST", "/api/setup", {"password": "geheim-1234"})
    assert code == 200


def wait_idle(c, timeout=15):
    end = time.time() + timeout
    while time.time() < end:
        code, st, _ = c.call("GET", "/api/status")
        if not st["busy"]:
            return st
        time.sleep(0.05)
    raise AssertionError("Lauf wurde nicht fertig")


def test_first_call_sets_password_and_logs_in(web):
    c, *_ = web
    assert c.call("GET", "/api/me")[1] == {"setup_needed": True, "authed": False, "min_password": 8}
    assert c.call("POST", "/api/setup", {"password": "kurz"})[0] == 400
    setup_login(c)
    me = c.call("GET", "/api/me")[1]
    assert me["setup_needed"] is False and me["authed"] is True
    assert c.call("POST", "/api/setup", {"password": "nochmal-1234"})[0] == 409        # only once


def test_status_needs_login_and_wrong_password_fails(web):
    c, *_ = web
    assert c.call("GET", "/api/status")[0] == 401
    setup_login(c)
    assert c.call("GET", "/api/status")[0] == 200
    c.call("POST", "/api/logout", {})
    assert c.call("GET", "/api/status")[0] == 401
    assert c.call("POST", "/api/login", {"password": "falsch-falsch"})[0] == 401
    assert c.call("POST", "/api/login", {"password": "geheim-1234"})[0] == 200
    assert c.call("GET", "/api/status")[0] == 200


def test_password_is_not_stored_in_clear(web):
    c, runner, srv, store, _ = web
    setup_login(c)
    rec = json.dumps(store.get("auth"))
    assert "geheim-1234" not in rec and "hash" in rec


def test_login_locks_after_repeated_failures(web):
    c, *_ = web
    setup_login(c)
    c.cookie = None
    codes = [c.call("POST", "/api/login", {"password": "x" * 9})[0] for _ in range(6)]
    assert codes[:5] == [401] * 5 and codes[5] == 429
    assert c.call("POST", "/api/login", {"password": "geheim-1234"})[0] == 429           # even the correct one stays blocked


def test_csrf_and_content_type_rules(web):
    c, *_ = web
    setup_login(c)
    assert c.call("POST", "/api/run", {}, csrf=False)[0] == 403
    assert c.call("POST", "/api/run", {}, ctype="text/plain")[0] == 415
    assert c.call("POST", "/api/run", {}, headers={"Origin": "http://evil.example"})[0] == 403


def test_static_files_and_path_traversal(web):
    c, *_ = web
    code, body, hdrs = c.call("GET", "/")
    assert code == 200 and b"<html" in body.lower() and "default-src 'self'" in hdrs["Content-Security-Policy"]
    assert c.call("GET", "/../config.py")[0] == 404
    assert c.call("GET", "/%2e%2e/config.py")[0] == 404
    assert c.call("GET", "/app.js")[0] == 200


def test_override_changes_plan_and_triggers_run(web):
    c, runner, srv, store, _ = web
    setup_login(c)
    st = wait_idle(c)
    arthur = next(i for i in st["items"] if i["title"].startswith("Auswertungsgespräch"))
    assert arthur["status"] == "geplant" and arthur["override_mode"] is None
    code, _, _ = c.call("POST", "/api/override", {"key": arthur["key"], "mode": None, "target": 90})
    assert code == 202
    st = wait_idle(c)
    arthur = next(i for i in st["items"] if i["title"].startswith("Auswertungsgespräch"))
    assert arthur["override_target"] == 90 and arthur["target_chain"] == 90
    assert st["desired"]["soc"] == 90
    assert any("/plan/soc/90/" in p for _, p in srv.calls)                  # configured live: was written to evcc
    # reset
    c.call("POST", "/api/override", {"key": arthur["key"], "mode": None, "target": None})
    st = wait_idle(c)
    assert st["desired"]["soc"] == 45


def test_override_no_car_and_validation(web):
    c, runner, srv, store, _ = web
    setup_login(c)
    st = wait_idle(c)
    arthur = next(i for i in st["items"] if i["title"].startswith("Auswertungsgespräch"))
    assert c.call("POST", "/api/override", {"key": arthur["key"], "mode": "x"})[0] == 400
    assert c.call("POST", "/api/override", {"key": arthur["key"], "mode": None, "target": 42})[0] == 400
    assert c.call("POST", "/api/override", {"key": arthur["key"], "mode": None, "target": True})[0] == 400
    assert c.call("POST", "/api/override", {"key": "gibt-es-nicht", "mode": "none"})[0] == 404
    assert c.call("POST", "/api/override", {"key": arthur["key"], "mode": "none"})[0] == 202
    st = wait_idle(c)
    assert next(i for i in st["items"] if i["key"] == arthur["key"])["status"] == "manuell_aus"
    assert st["desired"]["title"] != arthur["title"]


def test_event_without_address_can_be_planned_from_ui(web):
    from datetime import datetime, timezone
    c, runner, srv, store, _ = web
    runner.now_fn = lambda: datetime(2026, 10, 9, 10, 0, tzinfo=timezone.utc)
    runner.run()
    setup_login(c)
    st = wait_idle(c)
    omi = next(i for i in st["items"] if i["title"].startswith("Kinder bei Omi"))
    assert omi["status"] == "kein_ort" and omi["has_location"] is False
    assert c.call("POST", "/api/override", {"key": omi["key"], "mode": "car"})[0] == 202
    st = wait_idle(c)
    omi = next(i for i in st["items"] if i["key"] == omi["key"])
    assert omi["status"] == "ziel_fehlt"                                    # car without a place needs a target
    c.call("POST", "/api/override", {"key": omi["key"], "mode": "car", "target": 60})
    st = wait_idle(c)
    omi = next(i for i in st["items"] if i["key"] == omi["key"])
    assert omi["status"] == "geplant" and omi["manual"] and omi["target_chain"] == 60
    assert st["desired"]["soc"] == 60 and st["desired"]["time"] == "2026-10-11T10:45:00Z"   # 14:00 CEST = 12:00 UTC, minus 45 and 30 min


def test_personal_car_image_only_when_logged_in_and_falls_back(web, tmp_path):
    c, runner, srv, store, httpd = web
    cfg = runner.cfg
    cfg.personal_dir = str(tmp_path)
    assert c.call("GET", "/api/personal/car")[0] == 401                    # not logged in: never serve
    setup_login(c)
    assert c.call("GET", "/api/personal/car")[0] == 404                    # no custom image yet
    (tmp_path / "car.webp").write_bytes(b"RIFFxxxxWEBPfake")
    code, body, hdrs = c.call("GET", "/api/personal/car")
    assert code == 200 and hdrs["Content-Type"] == "image/webp" and body == b"RIFFxxxxWEBPfake"
    assert "private" in hdrs["Cache-Control"]
    c.call("POST", "/api/logout", {})
    assert c.call("GET", "/api/personal/car")[0] == 401


def test_personal_path_defaults_next_to_database():
    from evccplan.config import Config
    assert Config(db_path="/data/state.db").personal_path == "/data/personal"
    assert Config(db_path="/data/state.db", personal_dir="/x").personal_path == "/x"
