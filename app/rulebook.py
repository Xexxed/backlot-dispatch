"""Union-constraint rulebook: config-driven, clearly scoped, cited in the UI.

Defaults reflect a simplified subset of common SAG-AFTRA / IATSE-style
constraints used for decision support. The 1st AD can edit values via
rulebook.json (merged over defaults); every optimizer change cites its rule_id.
"""
from __future__ import annotations

import json
from pathlib import Path

from app.models import hhmm_to_minutes

DEFAULT_RULEBOOK: dict = {
    # Scene duration model: 1 script page ~= 1 minute of screen time,
    # plus fixed per-scene staging overhead (lighting tweaks, resets).
    "minutes_per_page": 1.0,
    "scene_overhead_minutes": 10,
    # Travel between different locations (fallback when no explicit matrix
    # entry exists in travels.csv).
    "location_move_buffer_minutes": 45,
    # Meal must START within this many minutes of the first work of the day
    # (simplified 6-hour turnaround/meal rule), and last at least
    # meal_break_minutes.
    "meal_within_minutes": 360,
    "meal_break_minutes": 30,
    # Daylight window for EXT DAY scenes (demo: fixed per production date).
    "daylight": {"sunrise": "06:30", "sunset": "19:15"},
    # Cast makeup/wardrobe lead added before a cast member's first scene.
    "cast_prep_buffer_minutes": 30,
}

_REQUIRED_INT_KEYS = (
    "minutes_per_page",
    "scene_overhead_minutes",
    "location_move_buffer_minutes",
    "meal_within_minutes",
    "meal_break_minutes",
    "cast_prep_buffer_minutes",
)


def load_rulebook(path: Path | None = None) -> dict:
    """Merge an optional rulebook.json over defaults and validate shapes."""
    rb = json.loads(json.dumps(DEFAULT_RULEBOOK))  # deep copy
    if path is not None and path.exists():
        overrides = json.loads(path.read_text(encoding="utf-8"))
        for key, value in overrides.items():
            rb[key] = value
    _validate(rb)
    return rb


def _validate(rb: dict) -> None:
    for key in _REQUIRED_INT_KEYS:
        if not isinstance(rb.get(key), (int, float)) or rb[key] <= 0:
            raise ValueError(f"rulebook.{key} must be a positive number")
    daylight = rb.get("daylight")
    if not isinstance(daylight, dict) or "sunrise" not in daylight or "sunset" not in daylight:
        raise ValueError("rulebook.daylight must define sunrise and sunset HH:MM")
    hhmm_to_minutes(daylight["sunrise"])  # raises on malformed time
    hhmm_to_minutes(daylight["sunset"])


class RuleBookContext:
    """Precomputed lookups shared by the timeline builder and validators."""

    def __init__(self, rulebook: dict, travel_times: dict[tuple[str, str], int] | None = None):
        self.rulebook = rulebook
        self.sunrise = hhmm_to_minutes(rulebook["daylight"]["sunrise"])
        self.sunset = hhmm_to_minutes(rulebook["daylight"]["sunset"])
        self.travel_fallback = int(rulebook["location_move_buffer_minutes"])
        # symmetric lookup keyed by unordered location-id pair
        self.travel_times: dict[frozenset[str], int] = {
            frozenset(pair): minutes for pair, minutes in (travel_times or {}).items()
        }

    def travel(self, loc_a: str, loc_b: str) -> int:
        if loc_a == loc_b:
            return 0
        return self.travel_times.get(frozenset((loc_a, loc_b)), self.travel_fallback)

    def scene_duration(self, page_count: float) -> int:
        import math

        return int(
            math.ceil(page_count * self.rulebook["minutes_per_page"])
            + self.rulebook["scene_overhead_minutes"]
        )
