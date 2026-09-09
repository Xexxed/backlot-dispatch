"""Cost & compliance ledger — pure pricing for recovery plans; zero I/O.

``price_plan`` derives every dollar from the plan's timeline rows and a
``RateCard`` (``seed/rates.csv`` via ``importers.load_rates``). It never
touches feasibility, diagnostics, or the publish gate — cost is advisory
display data only ("AI suggests, the engine decides, the dollar explains").

Model (v1, documented simplifications):
  * per-department on-clock span = last scene end involving the department
    minus first scene start involving it (meals included — the crew is on
    the clock through lunch);
  * Cast priced as one department (all cast members on their scenes' span);
  * hours past the first threshold bill overtime in bands: hours between
    tier 1 and tier 2 thresholds at tier 1's multiplier, hours beyond tier 2
    at tier 2's multiplier (union-style band math, priciest band last);
  * travel time is priced only via company moves (distinct location
    transitions × move cost), not as department hours;
  * penalty meals: lunch later than call + meal_within_minutes bills
    headcount × ceil(minutes_late / 30) × per-person penalty.

Absent timeline rows / unknown departments are handled loudly-but-safely:
unknown departments use the wildcard tier; malformed rows are skipped by the
caller's normalizer upstream (``serialize.slot_rows``).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from app.models import MEAL_ID, Production


@dataclass(frozen=True)
class RateTier:
    """One OT tier: past `threshold_hours`, hours bill at `multiplier`."""

    threshold_hours: float
    multiplier: float


@dataclass(frozen=True)
class RateCard:
    """Parsed seed/rates.csv: department rates + global penalty/move costs."""

    departments: dict[str, float]  # department -> hourly_rate
    ot_tiers: dict[str, list[RateTier]]  # department -> tiers sorted by threshold
    meal_penalty_per_person: float
    company_move_cost: float

    def rate_for(self, department: str) -> float:
        if department in self.departments:
            return self.departments[department]
        return self.departments["*"]

    def tiers_for(self, department: str) -> list[RateTier]:
        if department in self.ot_tiers:
            return self.ot_tiers[department]
        return self.ot_tiers["*"]


@dataclass
class CostBreakdown:
    """One plan's priced exposure — all figures display-only."""

    crew_hours: float = 0.0
    ot_hours: float = 0.0
    penalty_meals: int = 0  # half-hour units incurred
    penalty_people: int = 0
    company_moves: int = 0
    straight_cost: float = 0.0
    ot_cost: float = 0.0
    meal_penalty_cost: float = 0.0
    company_move_cost_total: float = 0.0
    total: float = 0.0
    delta_vs_baseline: float | None = None
    delta_vs_hold: float | None = None
    lines: list[str] = field(default_factory=list)


def _scene_departments(production: Production, item_id: str) -> list[str]:
    scene = production.scenes.get(item_id)
    if scene is None:
        return []
    if scene.departments:
        return list(scene.departments)
    # No department filter on the scene: everyone works it.
    return sorted({m.department for m in production.crew})


def _slot(row) -> tuple[str, int, int] | None:
    """Accept TimelineSlot objects or serialized dicts; None when unusable."""
    if hasattr(row, "item_id"):
        return row.item_id, int(row.start), int(row.end)
    if isinstance(row, dict):
        try:
            return str(row["item_id"]), int(row["start"]), int(row["end"])
        except (KeyError, TypeError, ValueError):
            return None
    return None


def price_plan(
    plan: dict,
    production: Production,
    rbc,
    rates: RateCard,
    hold_total: float | None = None,
    baseline_total: float | None = None,
) -> CostBreakdown:
    """Price one plan payload (or Proposal-serialized dict). Pure: same
    inputs → same breakdown; no I/O, no clock, no store."""
    out = CostBreakdown()
    rows = [s for s in (_slot(r) for r in plan.get("proposed_timeline") or []) if s]
    scene_rows = [(i, s, e) for (i, s, e) in rows if i != MEAL_ID]
    meal = next(((i, s, e) for (i, s, e) in rows if i == MEAL_ID), None)

    # --- company moves: distinct location transitions in shooting order ---
    transitions = 0
    prev_loc: str | None = None
    for item_id, _, _ in scene_rows:
        scene = production.scenes.get(item_id)
        if scene is None:
            continue
        if prev_loc is not None and scene.location_id != prev_loc:
            transitions += 1
        prev_loc = scene.location_id
    out.company_moves = transitions
    out.company_move_cost_total = transitions * rates.company_move_cost

    # --- per-department spans and OT ---
    dept_hours: dict[str, float] = {}
    for item_id, start, end in scene_rows:
        scene = production.scenes.get(item_id)
        if scene is None:
            continue
        span_hours = max(end - start, 0) / 60.0
        if scene.cast_ids:
            dept_hours["Cast"] = dept_hours.get("Cast", 0.0) + span_hours
        for department in _scene_departments(production, item_id):
            dept_hours[department] = dept_hours.get(department, 0.0) + span_hours

    headcount_total = len(production.crew) + len(production.cast)
    for department, hours in sorted(dept_hours.items()):
        out.crew_hours += hours
        out.straight_cost += hours * rates.rate_for(department)
        tiers = sorted(rates.tiers_for(department), key=lambda t: t.threshold_hours)
        ot = 0.0
        ot_cost = 0.0
        for i, tier in enumerate(tiers):
            if hours <= tier.threshold_hours:
                break
            band_start = tier.threshold_hours
            band_end = tiers[i + 1].threshold_hours if i + 1 < len(tiers) else None
            band_hours = hours - band_start if band_end is None else min(hours, band_end) - band_start
            if band_hours > 0:
                ot += band_hours
                ot_cost += band_hours * rates.rate_for(department) * tier.multiplier
        out.ot_hours += ot
        out.ot_cost += ot_cost

    # --- penalty meals: lunch later than call + meal_within_minutes ---
    deadline = production.call_time + int(rbc.rulebook["meal_within_minutes"])
    if meal is not None and meal[1] > deadline:
        late = meal[1] - deadline
        out.penalty_meals = (late + 29) // 30  # ceil to half-hour units
        out.penalty_people = headcount_total
        out.meal_penalty_cost = out.penalty_meals * headcount_total * rates.meal_penalty_per_person

    out.total = out.straight_cost + out.ot_cost + out.meal_penalty_cost + out.company_move_cost_total
    if baseline_total is not None:
        out.delta_vs_baseline = out.total - baseline_total
    if hold_total is not None:
        out.delta_vs_hold = out.total - hold_total

    # --- human-readable lines (production vocabulary) ---
    out.lines.append(
        f"Crew hours: {out.crew_hours:.1f}h straight time — ${out.straight_cost:,.0f}"
    )
    if out.ot_hours > 0:
        out.lines.append(
            f"Overtime: {out.ot_hours:.1f}h past turnaround thresholds — ${out.ot_cost:,.0f}"
        )
    out.lines.append(
        f"Company moves: {out.company_moves} × ${rates.company_move_cost:,.0f} — "
        f"${out.company_move_cost_total:,.0f}"
    )
    if out.penalty_meals > 0:
        out.lines.append(
            f"Penalty meals: {out.penalty_meals} half-hour unit(s) × "
            f"{out.penalty_people} people × ${rates.meal_penalty_per_person:,.0f} — "
            f"${out.meal_penalty_cost:,.0f}"
        )
    else:
        out.lines.append("Penalty meals: 0 — lunch inside the meal window")
    out.lines.append(f"Total exposure: ${out.total:,.0f}")
    return out
