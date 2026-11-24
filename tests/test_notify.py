"""Notification routing: categories, YAML-derived routes, validation, quiet hours, held notices."""
from datetime import datetime, timezone

import pytest

from evccplan import notify
from evccplan.config import Config
from evccplan.models import Notice
from evccplan.store import Store

UTC = timezone.utc
KNOWN = {"notify.a", "notify.b"}


def _n():
    cfg = Config(notify_targets={"info": ["notify.a"], "warning": ["notify.b"], "error": ["notify.a", "notify.b"]})
    return notify.Notifier(cfg, Store(":memory:")), cfg


def test_category_prefixes_and_fallback():
    for prefix, cat in notify.PREFIX.items():
        assert notify.category(prefix + ":2026", "warning") == cat
        assert notify.category(prefix, "warning") == cat
    assert notify.category("unknown:x", "info") == "plan"
    assert notify.category("unknown", "error") == "outage"
    assert notify.category("unknown", "warning") == "plan_warn"


def test_yaml_routes_follow_levels():
    r = notify.yaml_routes({"info": ["notify.a"], "error": ["notify.b"]})
    assert set(r) == set(notify.CATEGORIES)
    assert r["plan"] == ["notify.a"] and r["outage"] == ["notify.b"] and r["late"] == []


@pytest.mark.parametrize("data", [
    {"routes": {"nope": []}},
    {"routes": {"plan": ["notify.zzz"]}},
    {"routes": {"plan": ["light.a"]}},
    {"routes": {"plan": "notify.a"}},
    {"quiet": {"enabled": True, "from": "7:00", "to": "07:00"}},
    {"quiet": {"enabled": "yes", "from": "07:00", "to": "07:00"}},
    {"min_late_min": 61},
    {"min_late_min": True},
    {"other": 1},
])
def test_validate_rejects(data):
    with pytest.raises(ValueError):
        notify.validate(data, KNOWN)


def test_validate_rejects_too_many_entities():
    known = {"notify.e%d" % i for i in range(notify.MAX_ENTITIES + 1)}
    with pytest.raises(ValueError):
        notify.validate({"routes": {"plan": sorted(known)}}, known)


def test_validate_accepts_good_values():
    out = notify.validate({"routes": {"plan": ["notify.a", "notify.a"]}, "min_late_min": 3,
                           "quiet": {"enabled": False, "from": "22:00", "to": "07:00"}}, KNOWN)
    assert out["routes"] == {"plan": ["notify.a"]} and out["min_late_min"] == 3.0


def test_targets_use_yaml_until_custom_routes_saved_then_reset():
    ntf, cfg = _n()
    assert ntf.targets(Notice("overrun:x", "t", level="warning")) == ["notify.b"]
    ntf.update({"routes": {"late": ["notify.a"]}, "min_late_min": 7}, KNOWN)
    assert ntf.targets(Notice("overrun:x", "t", level="warning")) == ["notify.a"]
    assert ntf.targets(Notice("set:x", "t", level="info")) == []        # saved routes replace the YAML ones
    assert cfg.notify_min_late_min == 7
    ntf.reset()
    assert ntf.targets(Notice("overrun:x", "t", level="warning")) == ["notify.b"]
    assert cfg.notify_min_late_min == 2.0


def test_saved_min_late_is_applied_by_constructor():
    ntf, cfg = _n()
    ntf.update({"min_late_min": 9}, KNOWN)
    cfg2 = Config()
    notify.Notifier(cfg2, ntf.store)
    assert cfg2.notify_min_late_min == 9


def _quiet(ntf, a, b, on=True):
    ntf.update({"quiet": {"enabled": on, "from": a, "to": b}}, KNOWN)


def _at(h, m):
    return datetime(2026, 10, 12, h, m, tzinfo=UTC)


def test_in_quiet_across_midnight_in_local_time():
    ntf, _ = _n()                                                       # Europe/Berlin, CEST = UTC+2 in October
    _quiet(ntf, "22:00", "07:00")
    assert ntf.in_quiet(_at(21, 30))                                    # 23:30 local
    assert ntf.in_quiet(_at(4, 59))                                     # 06:59 local
    assert not ntf.in_quiet(_at(5, 0))                                  # 07:00 local
    assert not ntf.in_quiet(_at(12, 0))
    _quiet(ntf, "22:00", "07:00", on=False)
    assert not ntf.in_quiet(_at(21, 30))


def test_in_quiet_daytime_window():
    ntf, _ = _n()
    _quiet(ntf, "12:00", "14:00")
    assert ntf.in_quiet(_at(10, 0))                                     # 12:00 local
    assert not ntf.in_quiet(_at(12, 0))                                 # 14:00 local


def test_hold_replaces_same_key_caps_and_take_clears():
    ntf, _ = _n()
    ntf.hold(Notice("a", "first"))
    ntf.hold(Notice("a", "second", ttl_hours=3, level="info"))
    held = ntf.store.get("notify_held")
    assert len(held) == 1 and held[0] == {"key": "a", "text": "second", "level": "info", "ttl": 3}
    for i in range(notify.MAX_HELD + 5):
        ntf.hold(Notice("k%d" % i, "x"))
    held = ntf.store.get("notify_held")
    assert len(held) == notify.MAX_HELD and held[-1]["key"] == "k%d" % (notify.MAX_HELD + 4)
    assert len(ntf.take_held()) == notify.MAX_HELD
    assert ntf.take_held() == [] and ntf.store.get("notify_held") is None
