"""Optional MQTT link, one connection used in two directions:

* read: the retained evcc topics are turned back into the structure of evcc's /api/state, so the live
  status needs no polling (`MqttClient.state()` has the same contract as `EvccClient.state()`).
* publish: after every run the next plan is published as retained topics (e.g. for a Home Assistant card).

Plain MQTT 3.1.1 with user and password. The password only comes from the environment.
"""
from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

from .clients import ApiError

log = logging.getLogger("evccplan")

# evcc topics that are of no use here and may hold secrets (sponsor token, certificates, passwords)
DENY = ("site/sponsor", "site/eebus", "site/mqtt", "site/messagingEvents", "site/statistics", "site/influx",
        "site/ocpp", "site/remote", "site/network", "site/hems", "site/database", "site/modbusproxy")
SETTLE_S, SETTLE_MAX_S = 0.3, 3.0   # after connecting, wait until the retained topics stop arriving
KEEP_DICT = ("vehicles",)         # keyed by name, never turned into a list even if the name is a number


def _decode(payload) -> object:
    text = payload.decode("utf-8", "replace") if isinstance(payload, (bytes, bytearray)) else str(payload)
    if text == "":
        return None
    try:
        return json.loads(text)
    except ValueError:
        return text


def _listify(node, path: tuple = ()):
    """Dicts whose keys are all numbers (evcc counts lists from 1) become lists."""
    if not isinstance(node, dict):
        return node
    out = {k: _listify(v, path + (k,)) for k, v in node.items()}
    if out and path not in [(k,) for k in KEEP_DICT] and all(k.isdigit() for k in out):
        return [out[k] for k in sorted(out, key=int)]
    return out


def topics_to_state(flat: dict) -> dict:
    """{"site/pvPower": b"0", "loadpoints/1/chargePower": b"0", ...} -> the /api/state structure.

    "site/" is dropped (evcc's state has the site values at the top). A topic that has both a value and
    sub-topics is a count (`loadpoints` = 1) or an aggregate (`chargeCurrents`): the sub-topics win.
    """
    root: dict = {}
    for topic in sorted(flat, key=lambda t: t.count("/")):         # shallow topics first, deeper ones replace them
        parts = topic[5:].split("/") if topic.startswith("site/") else topic.split("/")
        if not all(parts):
            continue
        node = root
        for p in parts[:-1]:
            if not isinstance(node.get(p), dict):
                node[p] = {}
            node = node[p]
        if not isinstance(node.get(parts[-1]), dict):
            node[parts[-1]] = _decode(flat[topic])
    return _listify(root)


class MqttClient:
    def __init__(self, cfg, client_factory=None):
        self.cfg = cfg
        self._factory = client_factory
        self._client = None
        self._lock = threading.Lock()
        self._data: dict = {}
        self._connected = False
        self._was_connected = False
        self._connected_at = self._last_msg = 0.0
        self._error: Optional[str] = "MQTT is not started"
        self.changed = threading.Event()           # set on new data or a settings change (wakes the live poller)

    # ------------------------------------------------------------ connection
    def _active(self) -> bool:
        c = self.cfg
        return bool(c.mqtt_enabled and c.mqtt_host and (c.mqtt_read or c.mqtt_publish))

    def _prefix(self) -> str:
        return self.cfg.mqtt_topic.strip("/")

    def _pub_prefix(self) -> str:
        return self.cfg.mqtt_publish_topic.strip("/")

    def start(self) -> None:
        """Connects in the background (reconnects by itself). Without enabled and host nothing happens."""
        self.stop()
        self._was_connected = False
        if not self._active():
            self._error = "MQTT is off" if not self.cfg.mqtt_enabled else "MQTT host is missing"
            return
        c = self.cfg
        if self._factory:
            cl = self._factory()
        else:
            import paho.mqtt.client as paho
            cl = paho.Client(paho.CallbackAPIVersion.VERSION2, client_id="")
        if c.mqtt_user:
            cl.username_pw_set(c.mqtt_user, c.mqtt_password or None)
        if c.mqtt_publish:
            cl.will_set(self._pub_prefix() + "/status", "offline", qos=1, retain=True)
        cl.on_connect, cl.on_disconnect, cl.on_message = self._on_connect, self._on_disconnect, self._on_message
        cl.on_connect_fail = self._on_connect_fail
        cl.reconnect_delay_set(1, 120)
        self._error = "connecting"
        with self._lock:
            self._client = cl
        cl.connect_async(c.mqtt_host, c.mqtt_port, keepalive=60)
        cl.loop_start()

    def stop(self) -> None:
        with self._lock:
            cl, self._client = self._client, None
            self._connected, self._data = False, {}
        if cl is not None:
            try:
                if self.cfg.mqtt_publish and self._was_connected:
                    cl.publish(self._pub_prefix() + "/status", "offline", qos=1, retain=True)
                cl.disconnect()
                cl.loop_stop()
            except Exception:
                log.exception("MQTT: stop failed")
        self.changed.set()

    def reconfigure(self) -> None:
        """Applies changed settings (host, user, topics, on/off) without a restart."""
        self.start()

    # ------------------------------------------------------------ callbacks (paho thread)
    def _fail(self, text: str) -> None:
        if self._error != text:
            log.warning("MQTT: %s", text)
        self._error, self._connected = text, False

    def _on_connect(self, cl, userdata, flags, rc, props=None) -> None:
        if getattr(rc, "is_failure", False) or (isinstance(rc, int) and rc != 0):
            self._fail("connection refused (%s)" % rc)
            return
        with self._lock:
            self._data, self._connected, self._error = {}, True, None
            self._connected_at = self._last_msg = time.monotonic()
        self._was_connected = True
        log.info("MQTT: connected to %s:%s", self.cfg.mqtt_host, self.cfg.mqtt_port)
        if self.cfg.mqtt_read:
            p = self._prefix()
            for sub in ("site/#", "loadpoints/#", "vehicles/#"):
                cl.subscribe("%s/%s" % (p, sub), qos=0)
            cl.subscribe(p + "/status", qos=0)
        if self.cfg.mqtt_publish:
            cl.publish(self._pub_prefix() + "/status", "online", qos=1, retain=True)
        self.changed.set()

    def _on_disconnect(self, cl, userdata, *args) -> None:
        if self._connected:                                    # keep a more specific reason (e.g. login refused)
            self._fail("connection lost")
        self.changed.set()

    def _on_connect_fail(self, cl, userdata) -> None:
        self._fail("broker not reachable")

    def _on_message(self, cl, userdata, msg) -> None:
        p = self._prefix() + "/"
        if not msg.topic.startswith(p):
            return
        rel = msg.topic[len(p):]
        if rel.startswith(DENY):
            return
        with self._lock:
            self._data[rel] = msg.payload
            self._last_msg = time.monotonic()
        self.changed.set()

    # ------------------------------------------------------------ read
    def state(self) -> dict:
        """Same as EvccClient.state(), from the cache. ApiError without connection, data or with evcc offline."""
        while self._connected and time.monotonic() - self._connected_at < SETTLE_MAX_S \
                and time.monotonic() - self._last_msg < SETTLE_S:
            time.sleep(0.05)                                   # the initial burst of retained topics is still arriving
        with self._lock:
            ok, data, err = self._connected, dict(self._data), self._error
        if not self.cfg.mqtt_read:
            raise ApiError("reading from MQTT is off")
        if not ok:
            raise ApiError("MQTT: " + (err or "not connected"))
        status = data.pop("status", None)
        if not data:
            raise ApiError("MQTT: no data from evcc yet")
        if status is not None and _decode(status) != "online":
            raise ApiError("MQTT: evcc reports %s" % _decode(status))
        return topics_to_state(data)

    def error(self) -> Optional[str]:
        return None if self._connected else self._error

    # ------------------------------------------------------------ publish
    def publish_run(self, res: dict) -> None:
        """Publishes the outcome of a run as retained topics. Never raises."""
        if not (self.cfg.mqtt_publish and self._connected):
            return
        try:
            cl = self._client
            if cl is None:
                return
            for topic, value in run_topics(res).items():
                cl.publish("%s/%s" % (self._pub_prefix(), topic), value, qos=1, retain=True)
        except Exception:
            log.exception("MQTT: publish failed")


def run_topics(res: dict) -> dict:
    """topic (below the publish prefix) -> payload string. Plan topics are empty (clears the retained value) without a plan."""
    dz = res.get("desired") or {}
    act = res.get("action") or {}
    plan = {"title": dz.get("title"), "soc": dz.get("soc"), "time": dz.get("time"),
            "action": act.get("kind"), "dry_run": res.get("dry_run")}
    out = {"plan": json.dumps(plan, ensure_ascii=False) if dz else "",
           "plan/soc": "" if dz.get("soc") is None else str(dz["soc"]),
           "plan/time": dz.get("time") or "",
           "plan/title": dz.get("title") or "",
           "plan/action": act.get("kind") or "",
           "run/time": res.get("time") or "",
           "run/ok": "true" if res.get("ok") else "false",
           "run/dry_run": "true" if res.get("dry_run") else "false",
           "run/problems": str(len(res.get("problems") or []))}
    return out
