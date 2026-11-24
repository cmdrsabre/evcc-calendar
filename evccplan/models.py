from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Optional


class Mode(str, Enum):
    AUTO = "auto"
    BAHN = "bahn"


@dataclass(frozen=True)
class Event:
    uid: str
    title: str
    start: datetime          # aware, UTC
    end: datetime            # aware, UTC
    location: str = ""
    description: str = ""
    calendar: str = ""       # display name of the source calendar


@dataclass
class Classification:
    mode: Mode
    unclear: bool
    reason: str              # stable code: both | keyword_car | keyword_train | rule | default
    explicit: bool = False   # keyword car/train in the text (not rule/default)
    reason_arg: str = ""     # e.g. the matching rule pattern for reason == "rule"


@dataclass(frozen=True)
class Override:
    """Manual override for an event (from the web UI)."""
    mode: Optional[str] = None      # None = automatic | "car" = force car | "none" = no car
    target: Optional[int] = None    # target SoC in %, None = compute


def override_key(event: "Event") -> str:
    """Stable key per event instance (recurring events share the UID, hence the date is included)."""
    return "%s@%s" % (event.uid, event.start.strftime("%Y-%m-%d"))


@dataclass
class Place:
    lat: float
    lon: float
    label: str = ""
    confidence: float = 0.0
    layer: str = ""


@dataclass
class Route:
    distance_km: float       # one-way distance
    duration_min: float      # one-way travel time
    estimated: bool = False  # True = straight-line distance x factor instead of routing
    dest: Optional[tuple] = None   # (lat, lon) of the destination when known (needed for tour legs)


@dataclass
class Notice:
    key: str
    text: str
    ttl_hours: Optional[float] = None   # None = only once per key
    level: str = "warning"              # info | warning | error


@dataclass
class Item:
    event: Event
    cls: Optional[Classification] = None
    status: str = "neu"
    detail: str = ""
    route: Optional[Route] = None
    place_label: str = ""
    temp_c: Optional[float] = None
    kwh100: Optional[float] = None
    need_soc: Optional[float] = None
    ready_by: Optional[datetime] = None
    back_home: Optional[datetime] = None
    target_alone: Optional[int] = None
    target_chain: Optional[int] = None
    override: Optional[Override] = None
    manual: bool = False             # no route computed, target comes from the override
    # Tour = consecutive appointments driven directly from one to the next instead of via home.
    leg_in: Optional[Route] = None   # route from the previous appointment (follower of a tour only)
    leg_in_soc: Optional[float] = None
    leg_from: str = ""               # title of the previous appointment (follower only)
    arrival_soc: Optional[float] = None   # expected state of charge on arrival here (follower only)
    leg_home_soc: Optional[float] = None  # drive back home (last member of a tour only)
    tour_size: int = 1               # number of appointments in the tour (head only, 1 = no tour)
    tour_out_soc: Optional[float] = None    # head only: first leg from home
    tour_legs_soc: list = field(default_factory=list)   # head only: legs between the appointments
    tour_back_soc: Optional[float] = None   # head only: drive home after the last appointment
    tour_km: Optional[float] = None         # head only: total distance of the tour
    home_soc: Optional[float] = None        # expected state of charge when the unit (tour/appointment) is back home
    slack_min: float = 0.0           # charge buffer planned into ready_by (the real latest time is plan time + slack)


@dataclass
class Desired:
    item: Item
    soc: int
    time: datetime
    capped_from: Optional[int] = None
    over100: bool = False
    slack_min: float = 0.0


@dataclass
class Evaluation:
    items: list = field(default_factory=list)
    desired: Optional[Desired] = None
    notices: list = field(default_factory=list)
    skipped_reason: str = ""
