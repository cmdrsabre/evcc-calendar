"""Charge view: window, progress against the plan (ahead / behind), cost forecast, tolerance for odd evcc data."""
from datetime import datetime, timedelta, timezone

from evccplan import chargeview as cv
from evccplan.config import Config

NOW = datetime(2026, 10, 14, 22, 0, tzinfo=timezone.utc)
DEADLINE = NOW + timedelta(hours=9)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def state(lp=None, price=0.30, plan=True):
    veh = {"capacity": 54, "plan": {"soc": 85, "time": iso(DEADLINE)} if plan else None, "repeatingPlans": []}
    st = {"vehicles": {"db:1": veh}, "loadpoints": [dict(lp or {}, vehicleName="db:1")], "currency": "EUR"}
    if price is not None:
        st["tariffGrid"] = price
    return st


def view(lp=None, now=NOW, **kw):
    st = state(lp, **kw)
    raw = cv.extract(st, "db:1", now, st["loadpoints"][0])
    return cv.build(raw, Config(), now)


def test_away_estimates_window_and_cost_from_reserve():
    v = view({"connected": False})
    assert v["state"] == "away" and v["window"]["estimated"] is True and v["start_soc"] == 10
    # (85 - 10) % of 54 kWh = 40.5 kWh into the battery, 15 % losses -> 47.6 kWh from the grid at 0.30
    assert v["window"]["hours"] == 4.33
    assert v["cost"]["kwh"] == 47.6 and v["cost"]["max"] == 14.29 and v["cost"]["estimated_start"] is True
    assert v["progress"] is None
    assert v["window"]["end"] == iso(DEADLINE)


def test_connected_uses_real_soc_and_evcc_window():
    start, end = DEADLINE - timedelta(hours=3), DEADLINE - timedelta(minutes=5)
    v = view({"connected": True, "vehicleSoc": 60, "planProjectedStart": iso(start), "planProjectedEnd": iso(end)})
    assert v["state"] == "waiting" and v["window"]["estimated"] is False
    assert v["window"]["start"] == iso(start) and v["window"]["end"] == iso(end)
    assert v["cost"]["estimated_start"] is False and v["cost"]["max"] == round(25 / 100 * 54 / 0.85 * 0.30, 2)
    assert v["progress"] is None                                  # not started yet: waiting is not "ahead"


def window_lp(soc, now):
    # evcc window as it looks like when charging is on: started 1 h ago
    return {"connected": True, "charging": True, "vehicleSoc": soc,
            "planProjectedStart": iso(now - timedelta(hours=1)), "planProjectedEnd": iso(DEADLINE)}


def test_progress_ahead_on_plan_and_behind():
    now = DEADLINE - timedelta(hours=2)
    # rate: 11 kW * 0.85 / 54 kWh = 17.3 pp/h; 2 h left need 34.6 pp -> 50.4 % is exactly on plan
    on = view(window_lp(50.4, now), now=now)["progress"]
    ahead = view(window_lp(60, now), now=now)["progress"]
    behind = view(window_lp(40, now), now=now)["progress"]
    assert on["kind"] == "on" and abs(on["expected_soc"] - 50.4) < 0.5
    assert abs(behind["expected_soc"] - 50.4) < 0.5 and abs(ahead["expected_soc"] - 50.4) < 0.5     # "should be" is the same for all
    assert ahead["kind"] == "ahead" and 30 < ahead["minutes"] < 40       # (60 - 50.4) / 17.3 pp/h = 33 min
    assert behind["kind"] == "behind" and 35 < behind["minutes"] < 45     # (50.4 - 40) / 17.3 pp/h = 36 min
    assert view(window_lp(40, now), now=now)["state"] == "charging"
    lp = dict(window_lp(40, now), chargedEnergy=5900)                    # 5.9 kWh from the grid = 5.0 kWh into the battery = 9.3 pp
    v = view(lp, now=now)
    assert abs(v["cost"]["kwh"] - round((85 - (40 - 9.3)) / 100 * 54 / 0.85, 1)) < 0.2        # priced from the start of the plan


def test_done_when_target_reached():
    v = view({"connected": True, "vehicleSoc": 90})
    assert v["state"] == "done" and v["cost"] is None and v["progress"] is None


def test_no_price_from_evcc_means_no_cost():
    assert view({"connected": False}, price=None)["cost"] is None


def test_price_comes_from_the_grid_forecast_over_the_plan_slots_not_from_slot_values():
    a = {"start": iso(NOW), "end": iso(NOW + timedelta(hours=1)), "value": 10}              # plan slot values are cents: ignored
    b = {"start": iso(NOW + timedelta(hours=2)), "end": iso(NOW + timedelta(hours=5)), "value": 24}
    st = state({"connected": True, "vehicleSoc": 60, "plan": [b, a], "planProjectedStart": a["start"], "planProjectedEnd": b["end"]})
    ts = lambda h: int((NOW + timedelta(hours=h)).timestamp())
    st["forecast"] = {"grid": [[ts(-1), ts(3), 0.10], [ts(3), ts(9), 0.20]]}                # evcc format: [start, end, value]
    v = cv.build(cv.extract(st, "db:1", NOW, st["loadpoints"][0]), Config(), NOW)
    assert v["cost"]["price"] == 0.15 and v["cost"]["source"] == "forecast"                  # 0.10 for 1+1 h, 0.20 for 2 h
    assert [s["start"] for s in v["window"]["segments"]] == [a["start"], b["start"]]


def test_implausible_price_is_dropped():
    assert view({"connected": False}, price=24)["cost"] is None                              # cents instead of euro


def test_zero_time_from_evcc_is_ignored():
    zero = "0001-01-01T00:00:00Z"
    v = view({"connected": True, "vehicleSoc": 60, "planProjectedStart": zero, "planProjectedEnd": zero})
    assert v["window"]["estimated"] is True


def test_effective_plan_wins_and_repeating_plan_is_used():
    st = state({"connected": True, "vehicleSoc": 50, "effectivePlanTime": iso(DEADLINE + timedelta(hours=1)), "effectivePlanSoc": 70})
    raw = cv.extract(st, "db:1", NOW, st["loadpoints"][0])
    assert raw["plan"]["soc"] == 70
    st = state({"connected": False}, plan=False)
    st["vehicles"]["db:1"]["repeatingPlans"] = [{"active": True, "soc": 80, "time": "07:00", "tz": "UTC", "weekdays": [0, 1, 2, 3, 4, 5, 6]}]
    raw = cv.extract(st, "db:1", NOW, st["loadpoints"][0])
    assert raw["plan"]["soc"] == 80 and raw["plan"]["time"] > NOW


def test_no_plan_or_odd_data_gives_no_view():
    assert view({"connected": True}, plan=False) is None
    for bad in (None, [], "x", {"vehicles": []}, {"vehicles": {"db:1": {"capacity": "x"}}}):
        assert cv.extract(bad, "db:1", NOW, {}) == {}
    st = state({"connected": True, "vehicleSoc": "n/a", "planProjectedStart": 5, "plan": "oops"})
    assert cv.build(cv.extract(st, "db:1", NOW, st["loadpoints"][0]), Config(), NOW)["start_soc"] == 10


def test_max_cost_function():
    assert cv.max_cost(80, 80, 54, 0.15, 0.3)["max"] == 0.0
    assert cv.max_cost(80, 60, 54, 0.15, None) is None


def test_forecast_price_over_the_estimated_window():
    st = state({"connected": False})
    d = DEADLINE
    st["forecast"] = {"grid": [[int((d - timedelta(hours=10)).timestamp()), int((d - timedelta(hours=4)).timestamp()), 0.40],
                               [int((d - timedelta(hours=4)).timestamp()), int(d.timestamp()), 0.20]]}
    v = cv.build(cv.extract(st, "db:1", NOW, st["loadpoints"][0]), Config(), NOW)
    # window is 4.33 h long: 0.33 h at 0.40 and 4 h at 0.20
    assert v["cost"]["source"] == "forecast" and 0.20 < v["cost"]["price"] < 0.22


def solar_state(lp, hours_from_now=(0, 12), kw=8.0, home_w=500, scale=None):
    st = state(lp)
    st["homePower"] = home_w
    st["forecast"] = dict(st.get("forecast") or {})
    ts = lambda h: int((NOW + timedelta(hours=h)).timestamp())
    sol = {"timeseries": [[ts(h), kw * 1000 if hours_from_now[0] <= h <= hours_from_now[1] else 0] for h in range(-1, 24)]}
    if scale is not None:
        sol["scale"] = scale
    st["forecast"]["solar"] = sol
    return st


def test_pv_share_lowers_the_estimate_but_not_the_maximum():
    noon = NOW + timedelta(hours=2)                                       # evcc window of 1 h, PV 8 kW all that time
    lp = {"connected": True, "vehicleSoc": 60, "planProjectedStart": iso(noon), "planProjectedEnd": iso(noon + timedelta(hours=1))}
    st = solar_state(lp, hours_from_now=(0, 12))
    v = cv.build(cv.extract(st, "db:1", NOW, st["loadpoints"][0]), Config(), NOW)
    c = v["cost"]
    assert c["max"] == 4.76 and c["pv_kwh"] == 7.5                        # 1 h * min(11 kW, 8 - 0.5 kW); 15.9 kWh from the grid in total
    assert abs(c["est"] - 2.51) < 0.02


def test_pv_never_covers_more_than_the_charge_and_no_forecast_means_no_pv():
    lp = {"connected": True, "vehicleSoc": 60}
    st = solar_state(lp, kw=30.0)                                         # huge PV, window anywhere inside the forecast
    c = cv.build(cv.extract(st, "db:1", NOW, st["loadpoints"][0]), Config(), NOW)["cost"]
    assert c["pv_kwh"] == c["kwh"] and c["est"] < 0.05
    st = state(lp)                                                        # no solar forecast at all
    assert "pv_kwh" not in cv.build(cv.extract(st, "db:1", NOW, st["loadpoints"][0]), Config(), NOW)["cost"]


def test_solar_scale_only_reduces():
    assert cv._solar(solar_state({}, scale=1.4))[1] == 1.0
    assert cv._solar(solar_state({}, scale=0.7))[1] == 0.7
