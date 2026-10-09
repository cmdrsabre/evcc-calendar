from datetime import datetime, timedelta, timezone

import pytest

from evccplan import planner
from evccplan.classify import Rule
from evccplan.config import Config
from evccplan.models import Desired, Event, Item, Mode, Route

UTC = timezone.utc
NOW = datetime(2026, 10, 9, 10, 0, tzinfo=UTC)


def cfg(**kw):
    c = Config(rules=[Rule("firma*", Mode.BAHN), Rule("uni*", Mode.AUTO)], language="de")
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def ev(uid, title, start, hours=1.0, loc="Ort 1", desc=""):
    return Event(uid, title, start, start + timedelta(hours=hours), loc, desc)


def resolver(routes):
    def f(e):
        r = routes.get(e.uid)
        return (r, None, "X") if r else (None, "nicht gefunden", "")
    return f


def run(events, routes, c=None, cap=54, min_soc=30, temp=12.0, now=NOW):
    c = c or cfg()
    return planner.evaluate(events, now, c, cap, min_soc, resolver(routes), lambda dt: temp)


def test_single_event_target_and_time():
    start = datetime(2026, 10, 13, 8, 40, tzinfo=UTC)
    e = ev("a", "Klinik Uni", start)
    r = run([e], {"a": Route(47.4, 52)})
    d = r.desired
    assert d.soc == 50                                   # 35.1 % + 10 -> 45.1 -> 50
    assert d.time == datetime(2026, 10, 13, 7, 15, tzinfo=UTC)   # 08:40 - 52 min - 30 min = 07:18 -> 07:15
    assert r.items[0].status == "geplant"


def test_cold_uses_higher_consumption():
    e = ev("a", "Uni", datetime(2026, 10, 13, 8, 0, tzinfo=UTC))
    warm = run([e], {"a": Route(47.4, 52)}, temp=12).desired.soc
    cold = run([e], {"a": Route(47.4, 52)}, temp=5).desired.soc
    assert cold > warm


def test_below_min_soc_no_plan():
    e = ev("a", "Uni Auto", datetime(2026, 10, 13, 8, 0, tzinfo=UTC))     # keyword Auto forces car despite proximity
    r = run([e], {"a": Route(2.1, 8)})
    assert r.desired is None and r.items[0].status == "unter_min"


def test_short_distance_is_not_a_car_event():
    s = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)
    r = run([ev("a", "Arzt", s), ev("b", "Uni", s + timedelta(hours=5))], {"a": Route(2.1, 8), "b": Route(47.4, 52)})
    assert r.items[0].status == "nah" and r.items[0].need_soc is None
    assert r.items[1].status == "geplant"          # nearby event is not part of the chain


def test_no_location_and_bahn_ignored():
    s = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)
    r = run([ev("a", "Kinder bei Omi", s, loc=""), ev("b", "Max bei Firma!", s)], {"b": Route(45, 50)})
    assert r.desired is None
    assert [i.status for i in r.items] == ["kein_ort", "bahn"]


def test_unresolvable_address_notice_and_other_event_still_planned():
    s = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)
    r = run([ev("a", "Uni", s), ev("b", "Uni", s + timedelta(days=1))], {"b": Route(47.4, 52)})
    assert r.items[0].status == "adresse_unklar"
    assert any(n.key == "addr:a" for n in r.notices)
    assert r.desired.item.event.uid == "b"


def test_unclear_classification_caps_at_80_with_notice():
    e = ev("a", "Termin Auto Bahn", datetime(2026, 10, 13, 8, 0, tzinfo=UTC))      # contradictory -> unclear
    r = run([e], {"a": Route(100, 90)})                                    # need ~74 % + 10 -> 85
    assert r.desired.soc == 80 and r.desired.capped_from == 85
    assert any("Einstufung unklar" in n.text and "80 %" in n.text and "85 %" in n.text for n in r.notices)


def test_unclear_cap_silent_when_not_binding():
    e = ev("a", "Termin Auto Bahn", datetime(2026, 10, 13, 8, 0, tzinfo=UTC))
    r = run([e], {"a": Route(47.4, 52)})
    assert r.desired.soc == 50 and not r.notices


def test_clear_event_not_capped():
    e = ev("a", "Uni", datetime(2026, 10, 13, 8, 0, tzinfo=UTC))
    r = run([e], {"a": Route(100, 90)})
    assert r.desired.soc == 85 and r.desired.capped_from is None


def test_over_100_warns_and_plans_100():
    e = ev("a", "Uni", datetime(2026, 10, 13, 8, 0, tzinfo=UTC))
    r = run([e], {"a": Route(150, 120)})
    assert r.desired.soc == 100 and r.desired.over100
    assert any(n.key.startswith("over100:a") for n in r.notices)


def test_overlap_first_wins_with_warning():
    s = datetime(2026, 10, 13, 9, 0, tzinfo=UTC)
    r = run([ev("a", "Uni", s, hours=8), ev("b", "Uni", s + timedelta(hours=2), hours=1)],
            {"a": Route(45, 50), "b": Route(47, 50)})
    assert r.desired.item.event.uid == "a"
    assert r.items[1].status == "ueberschneidung"
    assert any(n.key.startswith("overlap") for n in r.notices)


def test_past_departure_skipped():
    e = ev("a", "Uni", NOW + timedelta(minutes=20))
    r = run([e], {"a": Route(47, 52)})
    assert r.items[0].status == "zu_spaet" and r.desired is None


def test_chain_raises_target_when_gap_too_short():
    a = ev("a", "Uni", datetime(2026, 10, 13, 8, 0, tzinfo=UTC), hours=1)
    # B starts shortly after: return from A about 10:00, departure for B about 10:20 -> hardly any charging time
    b = ev("b", "Uni", datetime(2026, 10, 13, 11, 40, tzinfo=UTC), hours=1)
    routes = {"a": Route(45, 50), "b": Route(45, 50)}
    r = run([a, b], routes)
    first = r.items[0]
    assert first.target_chain > first.target_alone
    assert r.desired.soc == first.target_chain
    assert r.items[1].status == "verkettet"


def test_chain_not_raised_with_long_gap():
    a = ev("a", "Uni", datetime(2026, 10, 13, 8, 0, tzinfo=UTC))
    b = ev("b", "Uni", datetime(2026, 10, 14, 8, 0, tzinfo=UTC))
    r = run([a, b], {"a": Route(45, 50), "b": Route(45, 50)})
    assert r.items[0].target_chain == r.items[0].target_alone
    assert r.items[1].status == "spaeter"            # more than 5 h gap: no follow-up event


def test_chain_beyond_window_is_later():
    a = ev("a", "Uni", datetime(2026, 10, 12, 8, 0, tzinfo=UTC))
    b = ev("b", "Uni", datetime(2026, 10, 14, 8, 0, tzinfo=UTC))
    r = run([a, b], {"a": Route(45, 50), "b": Route(45, 50)})
    assert r.items[1].status == "spaeter"


# ------------------------------------------------------------------ Context notices
def desired(soc, t, uid="a", title="Uni"):
    it = Item(event=ev(uid, title, t))
    return Desired(item=it, soc=soc, time=t)


def test_car_limit_warning():
    d = desired(80, NOW + timedelta(days=2))
    n = planner.context_notices(d, [], 80, cfg(), 54, NOW)
    assert len(n) == 1 and "80 %" in n[0].text
    assert planner.context_notices(d, [], 100, cfg(), 54, NOW) == []
    assert planner.context_notices(d, [], 0, cfg(), 54, NOW) == []       # unknown
    assert planner.context_notices(d, [], None, cfg(), 54, NOW) == []


def test_next_occurrence_weekday_mapping_sunday_zero():
    plan = {"time": "13:00", "tz": "Europe/Berlin", "weekdays": [0]}     # Sunday
    occ = planner.next_occurrence(plan, datetime(2026, 10, 9, 0, 0, tzinfo=UTC), datetime(2026, 10, 13, 0, 0, tzinfo=UTC))
    assert occ == datetime(2026, 10, 11, 11, 0, tzinfo=UTC)               # Sun 11.10. 13:00 CEST


def test_tight_repeating_plan_warns():
    t = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)                          # Saturday 14:00 CEST
    rep = [{"active": True, "soc": 50, "time": "13:00", "tz": "Europe/Berlin", "weekdays": [6]}]
    d = desired(90, t)
    n = planner.context_notices(d, rep, None, cfg(), 54, NOW)
    assert len(n) == 1 and "Zu enge Planung" in n[0].text


def test_repeating_plan_not_warned_when_inactive_roomy_or_higher():
    t = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    base = {"active": True, "soc": 50, "time": "13:00", "tz": "Europe/Berlin", "weekdays": [6]}
    d = desired(90, t)
    assert planner.context_notices(d, [dict(base, active=False)], None, cfg(), 54, NOW) == []
    assert planner.context_notices(d, [dict(base, soc=90)], None, cfg(), 54, NOW) == []
    assert planner.context_notices(d, [dict(base, time="06:00")], None, cfg(), 54, NOW) == []   # enough time
    assert planner.context_notices(d, [dict(base, weekdays=[2])], None, cfg(), 54, NOW) == []   # different day


# ------------------------------------------------------------------ Reconciliation
def z(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


T = NOW + timedelta(days=2)


def test_reconcile_cases():
    d = desired(60, T)
    assert planner.reconcile(d, None, None, NOW).kind == "set"
    same = {"soc": 60, "time": z(T)}
    assert planner.reconcile(d, same, None, NOW).kind == "none"
    mine_old = {"soc": 50, "time": z(T)}
    assert planner.reconcile(d, mine_old, mine_old, NOW).kind == "set"            # own plan -> update
    manual = {"soc": 90, "time": z(T + timedelta(hours=3))}
    assert planner.reconcile(d, manual, mine_old, NOW).kind == "manual"
    expired = {"soc": 90, "time": z(NOW - timedelta(days=1))}
    assert planner.reconcile(d, expired, None, NOW).kind == "set"


def test_reconcile_delete_only_own_plan():
    mine = {"soc": 50, "time": z(T)}
    assert planner.reconcile(None, mine, mine, NOW).kind == "delete"
    assert planner.reconcile(None, mine, None, NOW).kind == "none"                # foreign plan stays
    assert planner.reconcile(None, None, mine, NOW).kind == "none"
    assert planner.reconcile(None, {"soc": 50, "time": z(T + timedelta(hours=1))}, mine, NOW).kind == "none"


# ------------------------------------------------------------------ Overrides from the UI
from evccplan.models import Override, override_key


def run_ov(events, routes, ov, **kw):
    c = kw.pop("c", None) or cfg()
    return planner.evaluate(events, NOW, c, 54, 30, resolver(routes), lambda dt: 12.0, ov)


def test_override_none_excludes_event():
    e = ev("a", "Uni", datetime(2026, 10, 13, 8, 0, tzinfo=UTC))
    r = run_ov([e], {"a": Route(47.4, 52)}, {override_key(e): Override("none")})
    assert r.items[0].status == "manuell_aus" and r.desired is None


def test_override_car_beats_bahn_rule_and_nah_rule():
    s = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)
    b, n = ev("b", "Firma Besuch", s), ev("n", "Arzt", s + timedelta(days=1))
    ov = {override_key(b): Override("car"), override_key(n): Override("car", 60)}
    r = run_ov([b, n], {"b": Route(45, 50), "n": Route(1.5, 6)}, ov)
    assert r.items[0].status == "geplant"
    assert r.items[1].status != "nah"


def test_override_target_exact_without_reserve_rounding_and_no_unclear_cap():
    e = ev("a", "Termin Auto Bahn", datetime(2026, 10, 13, 8, 0, tzinfo=UTC))      # unclear
    r = run_ov([e], {"a": Route(100, 90)}, {override_key(e): Override(None, 95)})
    assert r.desired.soc == 95 and r.desired.capped_from is None
    assert r.items[0].target_alone is not None and r.items[0].target_alone != 95    # computed value stays visible


def test_event_without_location_and_target_is_planned_with_assumed_drive():
    s = datetime(2026, 10, 13, 8, 0, tzinfo=UTC)
    e = ev("a", "Besuch", s, loc="")
    r = run_ov([e], {}, {override_key(e): Override(None, 70)})
    assert r.desired.soc == 70
    assert r.desired.time == datetime(2026, 10, 13, 6, 45, tzinfo=UTC)     # 08:00 - 45 min - 30 min
    assert r.items[0].manual and r.items[0].route is None


def test_car_without_location_and_without_target_asks_for_target():
    e = ev("a", "Besuch", datetime(2026, 10, 13, 8, 0, tzinfo=UTC), loc="")
    r = run_ov([e], {}, {override_key(e): Override("car")})
    assert r.items[0].status == "ziel_fehlt" and r.desired is None
    assert any(n.key == "notarget:a" for n in r.notices)


def test_unresolvable_address_with_target_still_planned():
    e = ev("a", "Uni", datetime(2026, 10, 13, 8, 0, tzinfo=UTC))
    r = run_ov([e], {}, {override_key(e): Override(None, 60)})
    assert r.desired.soc == 60 and r.items[0].manual and not r.notices


def test_override_key_separates_recurring_instances():
    a = ev("same", "Kurs", datetime(2026, 10, 13, 8, 0, tzinfo=UTC))
    b = ev("same", "Kurs", datetime(2026, 10, 20, 8, 0, tzinfo=UTC))
    assert override_key(a) != override_key(b)


def test_chain_respects_fixed_target_of_later_event():
    a = ev("a", "Uni", datetime(2026, 10, 13, 8, 0, tzinfo=UTC), hours=1)
    b = ev("b", "Uni", datetime(2026, 10, 13, 11, 40, tzinfo=UTC), hours=1)         # hardly any charging time in between
    r = run_ov([a, b], {"a": Route(45, 50), "b": Route(45, 50)}, {override_key(b): Override(None, 100)})
    assert r.items[1].target_chain == 100
    assert r.items[0].target_chain > r.items[0].target_alone
