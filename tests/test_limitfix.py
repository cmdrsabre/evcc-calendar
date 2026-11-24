"""Raising the car's charge limit via Home Assistant (config ha.charge_limit)."""
from datetime import datetime, timedelta, timezone

from evccplan.clients import ApiError
from evccplan.config import from_dict
from evccplan.settings import validate

from test_e2e import env  # noqa: F401  (reuse fixture)

ENT = "number.ioniq_5_ac_charging_limit"


def _setup(env, limit=70):
    srv, cfg, runner, state, ors, store = env
    cfg.limit_entity = ENT
    srv.state["loadpoints"][0]["vehicleLimitSoc"] = limit
    return srv, cfg, runner, state, store


def _sets(srv):
    return [c for c in srv.calls if c[1] == "/api/services/number/set_value"]


def test_limit_raised_once_and_warning_mentions_it(env):
    srv, cfg, runner, state, store = _setup(env)
    res = runner.run()
    assert len(_sets(srv)) == 1
    assert store.get("limitfix")["from"] == 70
    assert res["vehicle"]["limit_fix"]["state"] == "pending"
    assert any("Ladelimit im Auto" in m and "wird versucht" in m for m in srv.notified)
    state["now"] += timedelta(hours=1)
    n = len(srv.notified)
    res = runner.run()
    assert len(_sets(srv)) == 1 and len(srv.notified) == n
    assert res["vehicle"]["limit_fix"]["state"] == "pending"


def test_limit_value_is_sent_to_ha(env):
    srv, cfg, runner, state, store = _setup(env)
    sent = []
    runner.ha.set_number = lambda e, v: sent.append((e, v))
    runner.run()
    assert sent == [(ENT, 100)]


def test_limit_retry_then_give_up(env):
    srv, cfg, runner, state, store = _setup(env)
    state["now"] = datetime(2026, 10, 11, 18, 0, tzinfo=timezone.utc)      # long before the plan time
    runner.run()
    state["now"] += timedelta(hours=3, minutes=1)
    runner.run()
    assert len(_sets(srv)) == 2
    assert any("immer noch" in m for m in srv.notified)
    state["now"] += timedelta(hours=3, minutes=1)
    runner.run()
    assert len(_sets(srv)) == 2
    assert any("nicht automatisch" in m for m in srv.notified)


def test_limit_confirmed_by_evcc(env):
    srv, cfg, runner, state, store = _setup(env)
    runner.run()
    srv.state["loadpoints"][0]["vehicleLimitSoc"] = 100
    state["now"] += timedelta(hours=1)
    res = runner.run()
    assert any("steht jetzt auf 100 %" in m for m in srv.notified)
    assert store.get("limitfix") is None
    assert res["vehicle"]["limit_fix"] is None


def test_limit_dry_run_sets_nothing(env):
    srv, cfg, runner, state, store = _setup(env)
    cfg.dry_run = True
    res = runner.run()
    assert not _sets(srv) and store.get("limitfix") is None
    assert res["vehicle"]["limit_fix"]["state"] == "dry"


def test_limit_ha_error_is_reported(env):
    srv, cfg, runner, state, store = _setup(env)

    def boom(entity, value):
        raise ApiError("HTTP 500")
    runner.ha.set_number = boom
    res = runner.run()
    assert res["vehicle"]["limit_fix"]["state"] == "failed"
    assert store.get("limitfix") is None
    assert any("fehlgeschlagen" in m for m in srv.notified)


def test_limit_off_without_entity_or_auto(env):
    srv, cfg, runner, state, store = _setup(env)
    cfg.limit_entity = ""
    res = runner.run()
    assert not _sets(srv) and res["vehicle"]["limit_fix"] is None
    assert any("Ladelimit im Auto" in m and "wird versucht" not in m for m in srv.notified)
    cfg.limit_entity, cfg.limit_auto = ENT, False
    runner.run()
    assert not _sets(srv)


def test_config_charge_limit_parsing():
    c = from_dict({"ha": {"charge_limit": {"entity": ENT, "value": 30, "auto": False}}})
    assert (c.limit_entity, c.limit_value, c.limit_auto) == (ENT, 50, False)
    assert from_dict({"ha": {"charge_limit": {"entity": "sensor.x"}}}).limit_entity == ""
    c = from_dict({})
    assert (c.limit_entity, c.limit_value, c.limit_auto) == ("", 100, True)


def test_limit_auto_setting_accepts_only_bool():
    assert validate({"limit_auto": False}) == {"limit_auto": False}
    for bad in (1, "true", None):
        try:
            validate({"limit_auto": bad})
        except ValueError:
            continue
        raise AssertionError(bad)
