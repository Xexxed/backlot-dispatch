"""View helpers: personal call times under baseline vs published plan."""
from __future__ import annotations

from typing import Any

from app.models import MEAL_ID, Production, TimelineSlot, minutes_to_hhmm


def _norm_rows(rows: list[Any]) -> list[dict]:
    """Normalize TimelineSlot objects or their serialized dicts to one shape."""
    out = []
    for r in rows:
        if isinstance(r, TimelineSlot):
            out.append(
                {
                    "item_id": r.item_id,
                    "start": r.start,
                    "end": r.end,
                    "is_meal": r.is_meal,
                }
            )
        else:
            out.append(
                {
                    "item_id": r["item_id"],
                    "start": r["start"],
                    "end": r["end"],
                    "is_meal": r["item_id"] == MEAL_ID,
                }
            )
    return out


def _first_scene_start(
    production: Production,
    kind: str,
    subject_id: str,
    rows: list[dict],
) -> tuple[int | None, str | None]:
    """Earliest start (+scene id) involving this person's department/cast role."""
    if kind == "cast":
        for r in rows:
            if not r["is_meal"] and subject_id in production.scenes[r["item_id"]].cast_ids:
                return r["start"], r["item_id"]
        return None, None
    member = next((m for m in production.crew if m.id == subject_id), None)
    if member is None:
        return None, None
    dept = member.department
    for r in rows:
        if r["is_meal"]:
            continue
        scene = production.scenes[r["item_id"]]
        if not scene.departments or dept in scene.departments:
            return r["start"], r["item_id"]
    return None, None


def compute_calls(
    production: Production,
    rbc,
    plan_payload: dict | None,
) -> dict[tuple[str, str], dict]:
    """Call-time cards for everyone: {(kind, subject_id): {...}}."""
    from app.timeline import PlanContext, build_timeline

    baseline_rows = _norm_rows(
        build_timeline(
            list(production.scene_order),
            PlanContext(
                production=production,
                rbc=rbc,
                earliest_start=production.call_time,
                day_anchor=production.call_time,
                blocked_windows=[],
            ),
        )
    )
    plan_rows = (
        _norm_rows(plan_payload["proposed_timeline"]) if plan_payload else []
    )
    prep = int(rbc.rulebook.get("cast_prep_buffer_minutes", 30))

    subjects: list[tuple[str, str]] = [("crew", m.id) for m in production.crew]
    subjects += [("cast", cid) for cid in production.cast]

    out: dict[tuple[str, str], dict] = {}
    for kind, sid in subjects:
        base_start, first_scene = _first_scene_start(production, kind, sid, baseline_rows)
        if base_start is None:
            base_start = production.call_time
        elif kind == "cast":
            base_start -= prep
        entry: dict[str, Any] = {
            "baseline_start": base_start,
            "baseline_hhmm": minutes_to_hhmm(base_start),
            "new_hhmm": None,
            "delta": 0,
            "delta_min": 0,
            "first_scene": first_scene,
        }
        if plan_rows:
            new_start, _ = _first_scene_start(production, kind, sid, plan_rows)
            if new_start is None:
                new_start = production.call_time
            if kind == "cast":
                new_start -= prep
            entry["new_hhmm"] = minutes_to_hhmm(new_start)
            entry["delta"] = new_start - base_start
            entry["delta_min"] = new_start - base_start
        out[(kind, sid)] = entry
    return out


def changes_for_person(
    changes: list[dict],
    production: Production,
    kind: str,
    subject_id: str,
) -> list[dict]:
    """Change entries that touch this person's scenes (or are global)."""
    relevant: list[dict] = []
    for c in changes:
        sid = c.get("scene_id")
        if sid is None:
            relevant.append(c)  # INFO / MEAL changes concern everyone
            continue
        scene = production.scenes.get(sid)
        if scene is None:
            continue
        if kind == "cast" and subject_id in scene.cast_ids:
            relevant.append(c)
        elif kind == "crew":
            member = next((m for m in production.crew if m.id == subject_id), None)
            if member and (not scene.departments or member.department in scene.departments):
                relevant.append(c)
    return relevant
