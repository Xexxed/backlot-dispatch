"""Serialization helpers: Proposal dataclass <-> JSON-safe dict (store + UI)."""
from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from app.models import MEAL_ID, Change, Diagnostic, Proposal, TimelineSlot, minutes_to_hhmm

if TYPE_CHECKING:  # annotation-only import keeps runtime dependency-light
    from app.costs import CostBreakdown


def proposal_to_dict(p: Proposal, status: str = "proposed") -> dict:
    data = asdict(p)
    data["status"] = status
    return data


def _slot_rows(items: list[Any]) -> list[dict]:
    """Normalize TimelineSlot objects or serialized slot dicts."""
    out = []
    for s in items:
        if isinstance(s, TimelineSlot):
            out.append(
                {
                    "item_id": s.item_id,
                    "start": s.start,
                    "end": s.end,
                    "location_id": s.location_id,
                    "label": s.label,
                }
            )
        else:
            out.append(
                {
                    "item_id": s["item_id"],
                    "start": s["start"],
                    "end": s["end"],
                    "location_id": s.get("location_id"),
                    "label": s.get("label", ""),
                }
            )
    return out


# Public alias: other pure modules (app.costs) consume the same normalizer —
# reuse, do not duplicate.
slot_rows = _slot_rows


def timeline_rows(items: list[Any], production) -> list[dict]:
    """UI-ready timeline rows with resolved names and HH:MM spans."""
    rows = []
    for s in _slot_rows(items):
        is_meal = s["item_id"] == MEAL_ID
        row = {
            "id": s["item_id"],
            "is_meal": is_meal,
            "start": s["start"],
            "end": s["end"],
            "span": f"{minutes_to_hhmm(s['start'])}–{minutes_to_hhmm(s['end'])}",
        }
        if is_meal:
            row.update({"label": "Lunch", "location": None, "location_name": None})
        else:
            scene = production.scenes[s["item_id"]]
            location = production.locations[scene.location_id]
            row.update(
                {
                    "label": f"{scene.id} · {scene.title}",
                    "location": scene.location_id,
                    "location_name": location.name,
                }
            )
        rows.append(row)
    return rows


def changes_view(changes: list[Change]) -> list[dict]:
    return [asdict(c) for c in changes]


def diagnostics_view(diagnostics: list[Diagnostic]) -> list[dict]:
    return [asdict(d) for d in diagnostics]


def option_stats(plan: dict, cost: "CostBreakdown | None" = None) -> dict:
    """Sandbox comparison metrics for one recovery-option payload.

    ``cost`` is an optional CostBreakdown (app.costs) — when supplied, the
    dollar fields ride along so sandbox cards, plan diff and dashboard all
    get them through this one funnel."""
    rows = _slot_rows(plan.get("proposed_timeline") or [])
    base_rows = _slot_rows(plan.get("baseline_timeline") or [])
    wrap = max((r["end"] for r in rows), default=plan.get("now_minutes") or 0)
    base_wrap = max((r["end"] for r in base_rows), default=wrap)
    lunch = next((r["start"] for r in rows if r["item_id"] == MEAL_ID), None)
    diagnostics = plan.get("diagnostics") or []
    delta = wrap - base_wrap
    out = {
        "strategy": plan.get("strategy") or "minimal",
        "status": plan.get("status", "proposed"),
        "moves": sum(1 for c in plan.get("changes") or [] if c.get("kind") == "MOVE"),
        "wrap_hhmm": minutes_to_hhmm(wrap),
        "wrap_delta": delta,
        "wrap_delta_display": (
            f"+{delta}m" if delta > 0 else (f"{delta}m" if delta < 0 else "on time")
        ),
        "lunch_hhmm": minutes_to_hhmm(lunch) if lunch is not None else None,
        "is_feasible": bool(plan.get("is_feasible")),
        "error_count": sum(1 for d in diagnostics if d.get("severity") == "ERROR"),
        "diagnostics": diagnostics,
        "changes": plan.get("changes") or [],
        "cost_total": None,
        "ot_hours": None,
        "penalty_meals": None,
        "company_moves": None,
        "delta_vs_hold_display": None,
    }
    if cost is not None:
        out["cost_total"] = round(cost.total, 2)
        out["ot_hours"] = round(cost.ot_hours, 2)
        out["penalty_meals"] = cost.penalty_meals
        out["company_moves"] = cost.company_moves
        if cost.delta_vs_hold is not None:
            d = cost.delta_vs_hold
            out["delta_vs_hold_display"] = (
                f"saves ${abs(d):,.0f} vs hold" if d < 0 else f"+${d:,.0f} vs hold"
            )
    return out
