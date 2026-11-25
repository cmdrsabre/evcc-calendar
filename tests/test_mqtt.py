"""MQTT link: topics -> evcc state, client callbacks without a network, live fallback, publishing, settings."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from evccplan.clients import ApiError
from evccplan.config import Config, from_dict
from evccplan.live import LivePoller, parse_state
from evccplan.mqtt import MqttClient, run_topics, topics_to_state
from evccplan.settings import validate

FIX = Path(__file__).resolve().parent / "fixtures"


def flat():
    return {k: v.encode() for k, v in json.loads((FIX / "mqtt_topics.json").read_text()).items()}


class FakePaho:
    def __init__(self):
        self.published, self.subs, self.will, self.calls = [], [], None, []

    def username_pw_set(self, u, p): self.calls.append(("auth", u, p))
    def will_set(self, topic, payload, qos=0, retain=False): self.will = (topic, payload, retain)
    def reconnect_delay_set(self, a, b): pass
    def connect_async(self, host, port, keepalive=60): self.calls.append(("connect", host, port))
    def loop_start(self): pass
    def loop_stop(self): pass
    def disconnect(self): pass
    def subscribe(self, topic, qos=0): self.subs.append(topic)
    def publish(self, topic, payload, qos=0, retain=False): self.published.append((topic, payload, retain))


def cfg(**kw):
    c = Config(mqtt_enabled=True, mqtt_host="broker", mqtt_user="u", mqtt_password="p")
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def client(**kw):
    fake = FakePaho()
    m = MqttClient(cfg(**kw), client_factory=lambda: fake)
    m.start()
    return m, fake


def connect(m, fake):
    m._on_connect(fake, None, None, 0)


def feed(m, topics, prefix="evcc"):
    for t, v in topics.items():
        m._on_message(None, None, SimpleNamespace(topic="%s/%s" % (prefix, t), payload=v))


def test_topics_to_state_matches_the_api_structure():
    st = topics_to_state({k: v for k, v in flat().items() if k != "status"})
    assert st["pvPower"] == 1500 and st["grid"]["power"] == -1100 and st["currency"] == "EUR"
    assert st["loadpoints"][0]["chargePower"] == 7400 and st["loadpoints"][0]["vehicleName"] == "db:1"
    assert st["loadpoints"][0]["plan"] is None and st["loadpoints"][0]["connected"] is True
    assert st["loadpoints"][0]["chargeCurrents"] == {"l1": 10, "l2": 10, "l3": 10}      # children win over the value
    assert st["vehicles"]["db:1"]["capacity"] == 54
    assert st["pv"] == [{"power": 1500}]
    assert st["forecast"]["grid"][0] == [1795640400, 1795641300, 0.2748]
    parsed = parse_state(st, "db:1")
    assert parsed["loadpoint"]["power_kw"] == 7.4 and parsed["site"] == {"pv_kw": 1.5, "grid_kw": -1.1, "home_kw": 0.4}


def test_topics_to_state_edge_cases():
    st = topics_to_state({"site/a": b"", "site/b": b"smart", "site/c": b"{bad", "vehicles/1/x": b"1", "site/l/2": b"b", "site/l/1": b"a"})
    assert st["a"] is None and st["b"] == "smart" and st["c"] == "{bad"
    assert st["vehicles"] == {"1": {"x": 1}}                    # vehicles stay a dict even with a numeric name
    assert st["l"] == ["a", "b"]
    assert topics_to_state({}) == {}


def test_state_needs_connection_data_and_online_status():
    m, fake = client()
    with pytest.raises(ApiError):
        m.state()                                              # not connected yet
    connect(m, fake)
    with pytest.raises(ApiError, match="no data"):
        m.state()
    feed(m, flat())
    assert m.state()["pvPower"] == 1500 and "sponsor" not in json.dumps(m.state()) and "status" not in m.state()
    feed(m, {"status": b"offline"})
    with pytest.raises(ApiError, match="offline"):
        m.state()


def test_secret_topics_are_dropped_and_reconnect_clears_the_cache():
    m, fake = client()
    connect(m, fake)
    feed(m, {"site/sponsor/status/token": b"x", "site/eebus/config/certificate/private": b"x", "site/mqtt/config/password": b"x", "site/pvPower": b"1"})
    assert list(m._data) == ["site/pvPower"]
    connect(m, fake)
    assert m._data == {}
    assert "evcc/site/#" in fake.subs and "evcc/loadpoints/#" in fake.subs and "evcc/status" in fake.subs


def test_refused_login_is_reported_and_lost_connection_marks_it_down():
    m, fake = client()
    m._on_connect(fake, None, None, SimpleNamespace(is_failure=True))
    assert "refused" in m.error()
    connect(m, fake)
    assert m.error() is None
    m._on_disconnect(fake, None)
    assert m.error() == "connection lost"
    assert ("auth", "u", "p") in fake.calls and ("connect", "broker", 1883) in fake.calls


def test_off_without_host_or_enabled_does_not_connect():
    for kw in ({"mqtt_enabled": False}, {"mqtt_host": ""}):
        m, fake = client(**kw)
        assert not any(c[0] == "connect" for c in fake.calls) and m.error()


def test_publish_after_a_run_and_last_will():
    m, fake = client(mqtt_publish=True, mqtt_read=False)
    assert fake.will == ("evcc-calendar/status", "offline", True)
    res = {"time": "2026-01-01T10:00:00Z", "ok": True, "dry_run": True, "problems": [],
           "desired": {"title": "Meeting", "soc": 55, "time": "2026-01-02T07:00:00Z"}, "action": {"kind": "set"}}
    m.publish_run(res)
    assert not [p for p in fake.published if p[0].startswith("evcc-calendar/plan")]      # not connected: nothing sent
    connect(m, fake)
    assert ("evcc-calendar/status", "online", True) in fake.published and not fake.subs   # read off: no subscriptions
    m.publish_run(res)
    sent = {t: v for t, v, r in fake.published}
    assert sent["evcc-calendar/plan/soc"] == "55" and sent["evcc-calendar/run/ok"] == "true"
    assert json.loads(sent["evcc-calendar/plan"])["title"] == "Meeting"
    assert all(r for _, _, r in fake.published)                                           # all retained
    assert run_topics({"time": "t", "ok": True, "desired": None})["plan"] == ""           # no plan: cleared


def test_live_poller_prefers_mqtt_and_falls_back_to_http():
    class Http:
        calls = 0
        def state(self):
            Http.calls += 1
            return {"pvPower": 100}
    m, fake = client()
    c = m.cfg
    p = LivePoller(Http(), c, mqtt=m)
    p.poll_once()                                              # MQTT not connected: HTTP, with the reason
    s = p.snapshot()
    assert s["source"] == "http" and s["mqtt_error"] and Http.calls == 1
    connect(m, fake); feed(m, flat())
    p.poll_once()
    s = p.snapshot()
    assert s["source"] == "mqtt" and s["mqtt_error"] is None and Http.calls == 1 and s["site"]["pv_kw"] == 1.5
    c.mqtt_read = False
    p.poll_once()
    assert p.snapshot()["source"] == "http" and Http.calls == 2


def test_config_and_settings():
    c = from_dict({"evcc": {"mqtt": {"enabled": True, "host": " b ", "port": 1884, "user": "u", "password_env": "PW",
                                     "topic": "/home/evcc/", "publish": True}}}, env=lambda n: "secret" if n == "PW" else "")
    assert (c.mqtt_enabled, c.mqtt_host, c.mqtt_port, c.mqtt_password, c.mqtt_topic, c.mqtt_publish, c.mqtt_read) == \
           (True, "b", 1884, "secret", "home/evcc", True, True)
    assert Config().mqtt_enabled is False
    assert from_dict({}, env=lambda n: "pw" if n == "EVCC_MQTT_PASSWORD" else "").mqtt_password == "pw"   # default variable name
    assert validate({"mqtt_host": " 192.168.1.2 ", "mqtt_port": 1883, "mqtt_topic": "a/b", "mqtt_enabled": True, "mqtt_user": ""}) == \
           {"mqtt_host": "192.168.1.2", "mqtt_port": 1883, "mqtt_topic": "a/b", "mqtt_enabled": True, "mqtt_user": ""}
    for bad in ({"mqtt_topic": "a/#"}, {"mqtt_topic": "a/+"}, {"mqtt_topic": ""}, {"mqtt_host": "a b"}, {"mqtt_host": 5},
                {"mqtt_port": 0}, {"mqtt_port": 70000}, {"mqtt_user": "a\nb"}, {"mqtt_enabled": "yes"}, {"mqtt_password": "x"}):
        with pytest.raises(ValueError):
            validate(bad)

