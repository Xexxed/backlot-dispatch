"""Serialization helpers: Proposal dataclass <-> JSON-safe dict (store + UI)."""
from __future__ import annotations

from dataclasses import asdict
from typing import Any

from app.models import MEAL_ID, Change, Diagnostic, Proposal, TimelineSlot, minutes_to_hhmm


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
