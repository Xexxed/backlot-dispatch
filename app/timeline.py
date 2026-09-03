"""Timeline construction shared by the optimizer and the portal views.

An "order" is a sequence of item ids: scene ids plus the optional MEAL_ID
sentinel. The builder walks items from a start clock, inserting travel buffers
between differing locations and computing per-scene durations from the
rulebook. Pure and deterministic — no LLM involved anywhere in this module.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.models import MEAL_ID, Production, Scene, TimelineSlot, minutes_to_hhmm
from app.rulebook import RuleBookContext


@dataclass
class BlockedWindow:
    """A location unavailable on [start, end) (e.g. generator down until 14:00)."""

    location_id: str
    start: int
    end: int


def spans_intersect(
    a_start: int, a_end: int, b_start: int, b_end: int
) -> bool:
    """Half-open interval intersection: [a_start, a_end) ∩ [b_start, b_end) ≠ ∅.

    The single overlap definition shared by the timeline builder's wait
    decision, the validator's blocked check, and the weather advisory.
    """
    return a_start < b_end and a_end > b_start


@dataclass
class PlanContext:
    production: Production
    rbc: RuleBookContext
    earliest_start: int  # 'now' on the shoot day; replanned work begins here
    day_anchor: int  # original crew call — meal-clock anchor
    blocked_windows: list[BlockedWindow]

    def is_blocked(self, location_id: str | None, start: int, end: int) -> bool:
        if location_id is None:
            return False
        return any(
            w.location_id == location_id and spans_intersect(start, end, w.start, w.end)
            for w in self.blocked_windows
        )

    def scene(self, scene_id: str) -> Scene:
        return self.production.scenes[scene_id]


def scene_duration(scene: Scene, rbc: RuleBookContext) -> int:
    return rbc.scene_duration(scene.page_count)


def build_timeline(order_items: list[str], ctx: PlanContext) -> list[TimelineSlot]:
    """Compute the wall-clock timeline for an ordered list of items.

    Blocked locations implicitly force a wait: work that would overlap the
    blocked window cannot start before the window clears, so the clock jumps
    to window end (the crew does prep/idles — physically what actually
    happens). Work that fully wraps before a future window opens is left
    alone (see Incident.blocked_from).
    """
    clock = ctx.earliest_start
    prev_location: str | None = None
    slots: list[TimelineSlot] = []
    for item_id in order_items:
        if item_id == MEAL_ID:
            duration = int(ctx.rbc.rulebook["meal_break_minutes"])
            slots.append(TimelineSlot(MEAL_ID, clock, clock + duration, None, "Lunch"))
            clock += duration
            continue
        scene = ctx.scene(item_id)
        duration = scene_duration(scene, ctx.rbc)
        # The travel lead-in is part of the span the scene will actually
        # occupy, so the wait decision below must include it — otherwise a
        # scene can be scheduled inside a FUTURE window it drives into.
        travel = 0
        if prev_location is not None and scene.location_id != prev_location:
            travel = ctx.rbc.travel(prev_location, scene.location_id)
        for w in ctx.blocked_windows:
            # Only intersecting the window forces a wait: a scene that fully
            # fits before a FUTURE block starts (blocked_from > now) shoots as
            # planned instead of idling until the window ends. Windows that
            # started at/before 'now' (start <= clock) keep legacy behavior —
            # the overlap test is then always true.
            if w.location_id == scene.location_id and spans_intersect(
                clock + travel, clock + travel + duration, w.start, w.end
            ):
                clock = w.end  # wait out the block; travel still applies after
        clock += travel
        slots.append(
            TimelineSlot(item_id, clock, clock + duration, scene.location_id, scene.title)
        )
        clock += duration
        prev_location = scene.location_id
    return slots


def clock_after(items: list[str], ctx: PlanContext) -> int:
    """Wall-clock minute at which the given item prefix finishes."""
    slots = build_timeline(items, ctx)
    return slots[-1].end if slots else ctx.earliest_start


def format_slot(span: tuple[int, int]) -> str:
    start, end = span
    return f"{minutes_to_hhmm(start)}–{minutes_to_hhmm(end)}"
