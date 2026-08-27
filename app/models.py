"""Core domain models.

Time convention: minutes-from-midnight ints internally; HH:MM strings at the
edges (CSV import, incident intake, UI rendering). Helpers below convert.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import Enum

from pydantic import BaseModel, Field


# ---------------------------------------------------------------- time utils
_HHMM_RE = re.compile(r"^(\d{1,2}):(\d{2})$")


def hhmm_to_minutes(value: str) -> int:
    """'07:30' -> 450. Raises ValueError on malformed input."""
    m = _HHMM_RE.match(value.strip())
    if not m:
        raise ValueError(f"invalid HH:MM time: {value!r}")
    hours, minutes = int(m.group(1)), int(m.group(2))
    if hours > 47 or minutes > 59:
        raise ValueError(f"time out of range: {value!r}")
    return hours * 60 + minutes


def minutes_to_hhmm(minutes: int) -> str:
    """450 -> '07:30'. Values >= 24h keep counting (e.g. 25:10)."""
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


# ------------------------------------------------------------------- enums
class IntExt(str, Enum):
    INT = "INT"
    EXT = "EXT"


class DayNight(str, Enum):
    DAY = "DAY"
    NIGHT = "NIGHT"


INCIDENT_TYPES = (
    "LOCATION_BLOCKED",
    "EQUIPMENT_FAILURE",
    "CAST_DELAY",
    "WEATHER",
    "OTHER",
)
SEVERITIES = ("low", "medium", "high")


# ------------------------------------------------------------------ entities
@dataclass
class Location:
    id: str
    name: str


@dataclass
class Scene:
    id: str
    title: str
    page_count: float
    location_id: str
    int_ext: IntExt
    day_night: DayNight
    cast_ids: list[str] = field(default_factory=list)
    departments: list[str] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)


@dataclass
class CrewMember:
    id: str
    name: str
    department: str
    role: str
    contact: str = ""


@dataclass
class CastMember:
    id: str
    name: str
    character: str = ""


@dataclass
class Production:
    """One production = one shooting day being managed (demo scope)."""

    id: str
    title: str
    shoot_date: str  # ISO date
    call_time: int  # minutes-from-midnight baseline crew call
    scenes: dict[str, Scene]
    scene_order: list[str]  # baseline order from the imported schedule
    crew: list[CrewMember]
    cast: dict[str, CastMember]
    locations: dict[str, Location]

    def scene(self, scene_id: str) -> Scene:
        return self.scenes[scene_id]

    @property
    def departments(self) -> list[str]:
        return sorted({c.department for c in self.crew})


# ------------------------------------------------------- incident (pydantic)
class Incident(BaseModel):
    """Structured disruption report produced by the intake agent or manual form."""

    type: str = Field(default="OTHER")
    location_id: str | None = None
    unit: str | None = None
    blocked_until: str | None = None  # HH:MM or None when not a blocking event
    severity: str = Field(default="medium")
    free_text: str = ""
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    source: str = "manual"  # gemini | manual_form | voice | fallback

    def blocked_until_minutes(self) -> int | None:
        if not self.blocked_until:
            return None
        return hhmm_to_minutes(self.blocked_until)


# --------------------------------------------- optimizer output structures
@dataclass
class Change:
    """One machine-checkable schedule change: rule_id + parameters."""

    scene_id: str | None  # None for whole-day items such as the meal move
    kind: str  # MOVE | HOLD | MEAL | INFO
    rule_id: str  # e.g. R-BLOCKED-LOCATION
    reason: str  # human-readable, generated from params (never by an LLM)
    params: dict = field(default_factory=dict)


@dataclass
class Diagnostic:
    severity: str  # ERROR | WARN
    rule_id: str
    message: str
    scene_ids: list[str] = field(default_factory=list)


MEAL_ID = "__MEAL__"  # sentinel scene id for the lunch break slot


@dataclass
class TimelineSlot:
    item_id: str  # scene id or MEAL_ID
    start: int
    end: int
    location_id: str | None
    label: str = ""

    @property
    def is_meal(self) -> bool:
        return self.item_id == MEAL_ID


@dataclass
class Proposal:
    """A proposed revised day awaiting AD review (the human gate)."""

    id: str
    created_at: str  # ISO timestamp
    incident: dict  # serialized Incident
    now_minutes: int  # demo-clock minute at which this replan was computed
    completed_scene_ids: list[str]
    baseline_order: list[str]
    proposed_order: list[str]  # scene ids only; meal position in meal_after_index
    meal_after_index: int  # insert MEAL after this many proposed scenes (-1 => none)
    baseline_meal_after_index: int
    changes: list[Change]
    diagnostics: list[Diagnostic]
    is_feasible: bool
    baseline_timeline: list[TimelineSlot]
    proposed_timeline: list[TimelineSlot]
    strategy: str = "minimal"  # recovery posture id (see engine.STRATEGIES)
    group_id: str = ""  # non-empty when part of a what-if sandbox option set

    def summary_counts(self) -> dict:
        return {
            "moves": sum(1 for c in self.changes if c.kind == "MOVE"),
            "diagnostics": len(self.diagnostics),
            "scenes_remaining": len(self.proposed_order),
        }
