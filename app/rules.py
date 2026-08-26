"""Hard-rule validators. Every violation cites a rule_id.

The optimizer uses these to prove feasibility; tests use them as independent
invariant checks; the portal renders them as diagnostics. One source of truth.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.models import MEAL_ID, Production
from app.timeline import PlanContext, TimelineSlot, build_timeline


@dataclass
class Violation:
    rule_id: str
    message: str
    scene_ids: list[str] = field(default_factory=list)


RULE_BLOCKED = "R-BLOCKED-LOCATION"
RULE_DAYLIGHT = "R-DAYLIGHT"
RULE_MEAL_WINDOW = "R-MEAL-WINDOW"
RULE_MEAL_BREAK = "R-MEAL-BREAK"
RULE_DEPS = "R-DEPS"
RULE_TRAVEL = "R-TRAVEL"


def check_blocked(slots: list[TimelineSlot], ctx: PlanContext) -> list[Violation]:
    out: list[Violation] = []
    for slot in slots:
        if slot.is_meal:
            continue
        if ctx.is_blocked(slot.location_id, slot.start, slot.end):
            window = next(w for w in ctx.blocked_windows if w.location_id == slot.location_id)
            out.append(
                Violation(
                    RULE_BLOCKED,
                    f"{slot.label} at {slot.location_id} overlaps blocked window "
                    f"ending {window.end}",
                    [slot.item_id],
                )
            )
    return out


def check_daylight(slots: list[TimelineSlot], ctx: PlanContext) -> list[Violation]:
    out: list[Violation] = []
    sunrise, sunset = ctx.rbc.sunrise, ctx.rbc.sunset
    for slot in slots:
        if slot.is_meal:
            continue
        scene = ctx.scene(slot.item_id)
        if scene.int_ext.value == "EXT" and scene.day_night.value == "DAY":
            if slot.start < sunrise or slot.end > sunset:
                out.append(
                    Violation(
                        RULE_DAYLIGHT,
                        f"EXT/DAY {slot.label} runs {slot.start}–{slot.end}, outside "
                        f"daylight {sunrise}–{sunset}",
                        [slot.item_id],
                    )
                )
    return out


def check_meal(slots: list[TimelineSlot], ctx: PlanContext) -> list[Violation]:
    rb = ctx.rbc.rulebook
    meal_slots = [s for s in slots if s.is_meal]
    scene_slots = [s for s in slots if not s.is_meal]
    out: list[Violation] = []
    if not scene_slots:
        return out
    deadline = ctx.day_anchor + int(rb["meal_within_minutes"])
    if not meal_slots:
        last_end = scene_slots[-1].end
        if last_end > deadline:
            out.append(
                Violation(
                    RULE_MEAL_WINDOW,
                    f"No meal break and work runs to {last_end}, past the "
                    f"{deadline} meal deadline",
                )
            )
        return out
    meal = meal_slots[0]
    if meal.start > deadline:
        out.append(
            Violation(
                RULE_MEAL_WINDOW,
                f"Lunch starts at {meal.start}, later than the {deadline} deadline "
                f"(call {ctx.day_anchor} + {rb['meal_within_minutes']}min)",
            )
        )
    if (meal.end - meal.start) < int(rb["meal_break_minutes"]):
        out.append(Violation(RULE_MEAL_BREAK, f"Lunch shorter than required break", [MEAL_ID]))
    return out


def check_dependencies(order: list[str], production: Production) -> list[Violation]:
    position = {sid: i for i, sid in enumerate(order)}
    out: list[Violation] = []
    for sid in order:
        scene = production.scenes[sid]
        for dep in scene.depends_on:
            if dep in position and position[dep] > position[sid]:
                out.append(
                    Violation(
                        RULE_DEPS,
                        f"Scene {sid} scheduled before its dependency {dep}",
                        [dep, sid],
                    )
                )
    return out


def check_travel(slots: list[TimelineSlot], ctx: PlanContext) -> list[Violation]:
    out: list[Violation] = []
    previous: TimelineSlot | None = None
    for slot in slots:
        if slot.is_meal:
            continue
        if (
            previous is not None
            and slot.location_id != previous.location_id
            and slot.start - previous.end < ctx.rbc.travel(previous.location_id or "", slot.location_id or "")
            and not _meal_between(previous, slot, slots)
        ):
            out.append(
                Violation(
                    RULE_TRAVEL,
                    f"Only {slot.start - previous.end}min between "
                    f"{previous.location_id} and {slot.location_id}",
                    [previous.item_id, slot.item_id],
                )
            )
        previous = slot
    return out


def _meal_between(a: TimelineSlot, b: TimelineSlot, slots: list[TimelineSlot]) -> bool:
    try:
        ia, ib = slots.index(a), slots.index(b)
    except ValueError:
        return False
    return any(s.is_meal for s in slots[ia + 1 : ib])


def validate_order(order_items: list[str], ctx: PlanContext) -> list[Violation]:
    """Full hard-rule validation of an ordered item list."""
    slots = build_timeline(order_items, ctx)
    scene_only = [i for i in order_items if i != MEAL_ID]
    out: list[Violation] = []
    out += check_blocked(slots, ctx)
    out += check_daylight(slots, ctx)
    out += check_meal(slots, ctx)
    out += check_dependencies(scene_only, ctx.production)
    out += check_travel(slots, ctx)
    return out
