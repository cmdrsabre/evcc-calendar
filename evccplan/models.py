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


@dataclass
class Classification:
    mode: Mode
    unclear: bool
    reason: str
    explicit: bool = False   # keyword car/train in the text (not rule/default)


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


@dataclass
class Desired:
    item: Item
    soc: int
    time: datetime
    capped_from: Optional[int] = None
    over100: bool = False


@dataclass
class Evaluation:
    items: list = field(default_factory=list)
    desired: Optional[Desired] = None
    notices: list = field(default_factory=list)
    skipped_reason: str = ""
