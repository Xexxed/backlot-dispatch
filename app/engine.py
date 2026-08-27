"""Deterministic re-optimizer — pure Python, zero LLM involvement.

Minimal-change heuristic over the remaining shooting day, parameterized by a
named recovery STRATEGY (see STRATEGIES / the what-if scenario sandbox):

  Pass A  R-BLOCKED-LOCATION : push affected scenes past the blocked window
  Pass B  R-DEPS             : restore dependency order after pushes/swaps
  Pass C  R-DAYLIGHT         : swap endangered EXT/DAY scenes with later INTs
  Pass D  R-MEAL-WINDOW      : place lunch so it starts before the deadline

Each strategy enables a different subset of passes over a different seed
order; passes repeat until stable (bounded). Anything still violating a hard
rule is surfaced as an explicit ERROR diagnostic via rules.validate_order —
this engine never returns a silent bad schedule. Every applied change carries
a machine-checkable rule_id.
"""
from __future__ import annotations

from dataclasses import dataclass

from app.models import (
    MEAL_ID,
    Change,
    Diagnostic,
    Incident,
    Production,
    Proposal,
    TimelineSlot,
    minutes_to_hhmm,
)
from app.rulebook import RuleBookContext
from app.rules import RULE_DAYLIGHT, validate_order
from app.timeline import BlockedWindow, PlanContext, build_timeline, clock_after

_MAX_CONVERGENCE_ROUNDS = 8

RULE_COVER_SET = "R-COVER-SET"


@dataclass(frozen=True)
class Strategy:
    """One named recovery posture: seed order + which repair passes run."""

    id: str
    name: str
    tagline: str
    description: str
    seed: str  # "pending" (original order) | "blocked_first" (stalled unit first)
    run_blocked_pass: bool
    run_deps_pass: bool
    run_daylight_pass: bool


STRATEGIES: dict[str, Strategy] = {
    "minimal": Strategy(
        id="minimal",
        name="Minimal change",
        tagline="Keep the day as close to the original plan as possible",
        description=(
            "Unblocked scenes keep their order and absorb the gap; blocked "
            "work moves behind them. Fewest scenes touched."
        ),
        seed="pending",
        run_blocked_pass=True,
        run_deps_pass=True,
        run_daylight_pass=True,
    ),
    "cover_set": Strategy(
        id="cover_set",
        name="Cover-set pivot",
        tagline="Resume the blocked location the moment it clears",
        description=(
            "The stalled unit shoots first once the block lifts; everyone "
            "else shifts later. Protects the blocked department's flow."
        ),
        seed="blocked_first",
        run_blocked_pass=False,
        run_deps_pass=True,
        run_daylight_pass=True,
    ),
    "hold": Strategy(
        id="hold",
        name="Hold & wait",
        tagline="No reordering — idle through the block",
        description=(
            "Nothing moves except lunch. The crew waits out the block and the "
            "day runs long — the measured cost of doing nothing."
        ),
        seed="pending",
        run_blocked_pass=False,
        run_deps_pass=False,
        run_daylight_pass=False,
    ),
}


# --------------------------------------------------------------------- utils
def _record(changes: list[Change], seen: set[tuple], change: Change) -> None:
    key = (change.scene_id, change.rule_id, change.kind)
    if key not in seen:
        seen.add(key)
        changes.append(change)


def _slot_for(slots: list[TimelineSlot], item_id: str) -> TimelineSlot:
    return next(s for s in slots if s.item_id == item_id)


def make_context(
    production: Production,
    rbc: RuleBookContext,
    earliest_start: int,
    blocked_windows: list[BlockedWindow] | None = None,
) -> PlanContext:
    return PlanContext(
        production=production,
        rbc=rbc,
        earliest_start=earliest_start,
        day_anchor=production.call_time,
        blocked_windows=blocked_windows or [],
    )


def baseline_context(production: Production, rbc: RuleBookContext) -> PlanContext:
    """The original plan's context: starts at crew call, nothing blocked."""
    return make_context(production, rbc, production.call_time)


# -------------------------------------------------------------------- passes
def _repair_blocked(order: list[str], ctx: PlanContext) -> tuple[list[str], list[Change]]:
    """Move blocked-location work behind the rest of the day (stable partition).

    The timeline builder makes scenes at a blocked location WAIT for the
    window to clear, so this pass only needs to reorder once: unblocked work
    keeps its relative order, blocked work follows it in original order.
    Deterministic, minimal-disruption, and immune to the remove/shift ping-pong
    an incremental bubble would cause.
    """
    changes: list[Change] = []
    if not ctx.blocked_windows:
        return order, changes
    blocked_locations = {w.location_id for w in ctx.blocked_windows}
    affected = [s for s in order if ctx.scene(s).location_id in blocked_locations]
    if not affected:
        return order, changes

    others = [s for s in order if ctx.scene(s).location_id not in blocked_locations]
    candidate = others + affected
    slots = build_timeline(candidate, ctx)
    new_start_by_scene = {s.item_id: s.start for s in slots}
    window_end = max(w.end for w in ctx.blocked_windows)
    for sid in affected:
        old_pos = order.index(sid)
        new_pos = candidate.index(sid)
        if old_pos == new_pos:
            continue
        changes.append(
            Change(
                scene_id=sid,
                kind="MOVE",
                rule_id="R-BLOCKED-LOCATION",
                reason=(
                    f"Scene {sid} moved behind unblocked work — "
                    f"{sorted(blocked_locations)[0]} unavailable until "
                    f"{minutes_to_hhmm(window_end)}"
                ),
                params={
                    "new_start": new_start_by_scene.get(sid),
                    "blocked_until": window_end,
                    "location": sorted(blocked_locations)[0],
                },
            )
        )
    order[:] = candidate
    return order, changes


def _repair_dependencies(
    order: list[str], production: Production
) -> tuple[list[str], list[Change]]:
    """Ensure every dependency appears earlier in the order."""
    changes: list[Change] = []
    changed = True
    guard = len(order) + 5
    while changed and guard > 0:
        changed = False
        guard -= 1
        position = {sid: i for i, sid in enumerate(order)}
        for sid in list(order):
            moved = False
            for dep in production.scenes[sid].depends_on:
                j = position.get(dep)
                if j is not None and j > position[sid]:
                    order.remove(sid)
                    order.insert(order.index(dep) + 1, sid)
                    position = {sid2: k for k, sid2 in enumerate(order)}
                    changes.append(
                        Change(
                            scene_id=sid,
                            kind="MOVE",
                            rule_id="R-DEPS",
                            reason=f"Scene {sid} moved after its dependency {dep}",
                            params={"depends_on": dep},
                        )
                    )
                    moved = True
                    break
            if moved:
                changed = True
                break
    return order, changes


def _repair_daylight(order: list[str], ctx: PlanContext) -> tuple[list[str], list[Change]]:
    """Swap daylight-endangered EXT/DAY scenes forward with nearby INT scenes."""
    changes: list[Change] = []
    sunrise, sunset = ctx.rbc.sunrise, ctx.rbc.sunset
    guard = len(order) + 2
    while guard > 0:
        guard -= 1
        slots = build_timeline(order, ctx)
        target = None
        for slot in slots:
            if slot.is_meal:
                continue
            scene = ctx.scene(slot.item_id)
            if (
                scene.int_ext.value == "EXT"
                and scene.day_night.value == "DAY"
                and (slot.start < sunrise or slot.end > sunset)
            ):
                target = slot
                break
        if target is None:
            break
        i_target = order.index(target.item_id)
        swap_with = next(
            (
                j
                for j in range(i_target + 1, len(order))
                if ctx.scene(order[j]).int_ext.value == "INT"
            ),
            None,
        )
        if swap_with is None:
            break  # nothing to swap with; validator raises the diagnostic
        partner = order[swap_with]
        order[i_target], order[swap_with] = partner, target.item_id
        new_slot = _slot_for(build_timeline(order, ctx), target.item_id)
        changes.append(
            Change(
                scene_id=target.item_id,
                kind="MOVE",
                rule_id=RULE_DAYLIGHT,
                reason=(
                    f"Scene {target.item_id} swapped ahead of {partner} to shoot "
                    f"in daylight ({minutes_to_hhmm(new_slot.start)}–"
                    f"{minutes_to_hhmm(new_slot.end)})"
                ),
                params={"swapped_with": partner, "new_start": new_slot.start},
            )
        )
    return order, changes


def _meal_index(order: list[str], ctx: PlanContext) -> int:
    """Latest insertion point keeping lunch start within the union window."""
    deadline = ctx.day_anchor + int(ctx.rbc.rulebook["meal_within_minutes"])
    for k in range(len(order), -1, -1):
        if clock_after(order[:k], ctx) <= deadline:
            return k
    return 0


def _diagnose(items: list[str], ctx: PlanContext) -> list[Diagnostic]:
    """Residual hard-rule violations become explicit ERROR diagnostics."""
    out: dict[str, Diagnostic] = {}
    for v in validate_order(items, ctx):
        out[v.message] = Diagnostic(
            severity="ERROR", rule_id=v.rule_id, message=v.message, scene_ids=v.scene_ids
        )
    return list(out.values())


# --------------------------------------------------------------------- entry
def _cover_set_seed(
    order: list[str], ctx: PlanContext, changes: list[Change], seen: set[tuple]
) -> list[str]:
    """Blocked-location scenes shoot FIRST once the window clears.

    The timeline builder makes them wait for the window (clock jump), so the
    stalled unit resumes the moment the location reopens; everyone else
    follows. Seed changes are recorded under R-COVER-SET.
    """
    if not ctx.blocked_windows:
        return order
    blocked_locations = {w.location_id for w in ctx.blocked_windows}
    affected = [s for s in order if ctx.scene(s).location_id in blocked_locations]
    if not affected:
        return order
    others = [s for s in order if ctx.scene(s).location_id not in blocked_locations]
    candidate = affected + others
    if candidate == order:
        return order
    slots = build_timeline(candidate, ctx)
    window_end = max(w.end for w in ctx.blocked_windows)
    for sid in affected:
        new_start = next(s.start for s in slots if s.item_id == sid)
        _record(
            changes,
            seen,
            Change(
                scene_id=sid,
                kind="MOVE",
                rule_id=RULE_COVER_SET,
                reason=(
                    f"Scene {sid} moved ahead of unblocked work — "
                    f"{sorted(blocked_locations)[0]} reopens "
                    f"{minutes_to_hhmm(window_end)}; stalled unit resumes "
                    f"immediately"
                ),
                params={"new_start": new_start, "blocked_until": window_end},
            ),
        )
    return candidate


def replan(
    production: Production,
    rbc: RuleBookContext,
    completed_scene_ids: list[str],
    incident: Incident,
    now_minutes: int,
    plan_id: str,
    created_at: str,
    strategy: str = "minimal",
    group_id: str = "",
) -> Proposal:
    strat = STRATEGIES.get(strategy)
    if strat is None:
        raise ValueError(f"unknown strategy: {strategy!r}")
    done = set(completed_scene_ids)
    pending = [sid for sid in production.scene_order if sid not in done]

    base_ctx = baseline_context(production, rbc)

    windows: list[BlockedWindow] = []
    if incident.location_id:
        until = incident.blocked_until_minutes()
        if until is not None:
            windows.append(
                BlockedWindow(
                    location_id=incident.location_id,
                    start=min(now_minutes, until),
                    end=until,
                )
            )
    ctx = make_context(
        production,
        rbc,
        earliest_start=max(now_minutes, production.call_time),
        blocked_windows=windows,
    )

    changes: list[Change] = []
    seen: set[tuple] = set()
    if windows:
        w = windows[0]
        _record(
            changes,
            seen,
            Change(
                scene_id=None,
                kind="INFO",
                rule_id="R-BLOCKED-LOCATION",
                reason=(
                    f"Incident: {w.location_id} blocked "
                    f"{minutes_to_hhmm(w.start)}–{minutes_to_hhmm(w.end)} "
                    f"({incident.type}, severity {incident.severity})"
                ),
                params={"location": w.location_id, "until": w.end},
            ),
        )

    order = list(pending)
    if pending:
        if strat.seed == "blocked_first":
            order = _cover_set_seed(order, ctx, changes, seen)

        enabled = (
            strat.run_blocked_pass,
            strat.run_deps_pass,
            strat.run_daylight_pass,
        )
        if any(enabled):
            for _ in range(_MAX_CONVERGENCE_ROUNDS):
                snapshot = tuple(order)
                c_block: list[Change] = []
                c_deps: list[Change] = []
                c_day: list[Change] = []
                if strat.run_blocked_pass:
                    order, c_block = _repair_blocked(order, ctx)
                if strat.run_deps_pass:
                    order, c_deps = _repair_dependencies(order, production)
                if strat.run_daylight_pass:
                    order, c_day = _repair_daylight(order, ctx)
                for c in c_block + c_deps + c_day:
                    _record(changes, seen, c)
                if tuple(order) == snapshot:
                    break

        k_meal = _meal_index(order, ctx)
        k_baseline = _meal_index(pending, base_ctx)
        # Only report a meal move when something actually changed operationally;
        # identical order + no block means the same physical lunch slot stands.
        if k_meal != k_baseline and (windows or order != pending):
            meal_start = clock_after(order[:k_meal], ctx)
            _record(
                changes,
                seen,
                Change(
                    scene_id=None,
                    kind="MEAL",
                    rule_id="R-MEAL-WINDOW",
                    reason=(
                        f"Lunch rescheduled to start {minutes_to_hhmm(meal_start)} "
                        f"to stay within "
                        f"{int(ctx.rbc.rulebook['meal_within_minutes'])}min of crew call"
                    ),
                    params={"new_meal_start": meal_start},
                ),
            )
    else:
        k_meal = 0
        k_baseline = 0

    proposed_items = order[:k_meal] + [MEAL_ID] + order[k_meal:]
    baseline_items = pending[:k_baseline] + [MEAL_ID] + pending[k_baseline:]

    diagnostics = _diagnose(proposed_items, ctx)
    is_feasible = not any(d.severity == "ERROR" for d in diagnostics)

    return Proposal(
        id=plan_id,
        created_at=created_at,
        incident=incident.model_dump(),
        now_minutes=now_minutes,
        completed_scene_ids=sorted(done),
        baseline_order=pending,
        proposed_order=order,
        meal_after_index=k_meal,
        baseline_meal_after_index=k_baseline,
        changes=changes,
        diagnostics=diagnostics,
        is_feasible=is_feasible,
        baseline_timeline=build_timeline(baseline_items, base_ctx),
        proposed_timeline=build_timeline(proposed_items, ctx),
        strategy=strat.id,
        group_id=group_id,
    )
