"""Pure calculation functions for consumption, need and target values."""
from __future__ import annotations

import math
from typing import Optional


def kwh_per_100(temp_c: Optional[float], warm: float, cold: float, threshold_c: float) -> float:
    """Unknown temperature is conservatively treated as cold."""
    if temp_c is None:
        return cold
    return warm if temp_c >= threshold_c else cold


def round_trip_need_soc(one_way_km: float, kwh100: float, capacity_kwh: float) -> float:
    """Need for the round trip in percentage points of state of charge."""
    return 2.0 * one_way_km * kwh100 / 100.0 / capacity_kwh * 100.0


def ceil5(x: float) -> int:
    return int(math.ceil(round(x, 6) / 5.0)) * 5


def charge_gain_soc(hours: float, capacity_kwh: float, power_kw: float, loss_margin: float) -> float:
    """State-of-charge gain in percentage points that can be charged at home within `hours`."""
    if hours <= 0:
        return 0.0
    return hours * power_kw * (1.0 - loss_margin) / capacity_kwh * 100.0


def hours_to_charge(delta_soc: float, capacity_kwh: float, power_kw: float, loss_margin: float) -> float:
    if delta_soc <= 0:
        return 0.0
    return delta_soc / 100.0 * capacity_kwh / (power_kw * (1.0 - loss_margin))
