"""Deterministic AD schedule edits — structured intents, zero LLM.

The editor agent (or the manual fallback form) converts the AD's request into
EditIntent objects; THIS module is the only place the schedule is mutated.
Every applied intent becomes a machine-checkable Change (rule_id
R-EDIT-INTENT) in the proposal's change log, and the edited day then flows
through the same repair passes and hard-rule validation as incident recovery —
an edit can never produce a silent bad schedule. Structurally invalid intents
raise a loud, AD-facing EditError instead of guessing.
"""
from __future__ import annotations

from copy import deepcopy

from app.engine import replan
from app.models import (
    EDIT_ACTIONS,
    Change,
    DayNight,
    EditIntent,
    Incident,
    IntExt,
    Production,
    Proposal,
    Scene,
)
from app.rulebook import RuleBookContext

RULE_EDIT_INTENT = "R-EDIT-INTENT"


class EditError(Exception):
    """Structurally invalid edit intent; the message is AD-facing."""


def _require_pending_scene(order: list[str], scenes: dict, scene_id: str | None, seq: int) -> None:
    if not scene_id:
        raise EditError(f"Edit {seq}: no scene selected.")
    if scene_id not in scenes:
        raise EditError(f"Edit {seq}: unknown scene {scene_id!r}.")
    if scene_id not in order:
        raise EditError(
            f"Edit {seq}: scene {scene_id} is not in the remaining day (already shot?)."
        )


def apply_edits(
    production: Production,
    rbc: RuleBookContext,
    completed_scene_ids: list[str],
    edits: list[EditIntent],
    now_minutes: int,
    plan_id: str,
    created_at: str,
    strategy: str = "minimal",
    incident: Incident | None = None,
) -> Proposal:
    """Apply structured edit intents deterministically and return a Proposal
    that flows through the standard review gate."""
    if not edits:
        raise EditError("No edits requested — nothing to apply.")
    for e in edits:
        if e.action not in EDIT_ACTIONS:
            raise EditError(f"Unsupported edit action {e.action!r}.")

    work = deepcopy(production)  # never mutate the shared production instance
    done = set(completed_scene_ids)
    order = [sid for sid in work.scene_order if sid not in done]
    changes: list[Change] = []

    for seq, intent in enumerate(edits, start=1):
        if intent.action == "move_scene":
            _require_pending_scene(order, work.scenes, intent.scene_id, seq)
            ref = intent.ref_scene_id
            if ref == intent.scene_id:
                raise EditError(f"Edit {seq}: a scene cannot be placed after itself.")
            if ref is not None:
                if ref not in work.scenes:
                    raise EditError(f"Edit {seq}: unknown reference scene {ref!r}.")
                if ref not in order:
                    raise EditError(
                        f"Edit {seq}: reference scene {ref} is not in the remaining day."
                    )
            order.remove(intent.scene_id)
            order.insert(order.index(ref) + 1 if ref else len(order), intent.scene_id)
            where = f"after {ref}" if ref else "to the end of the day"
            changes.append(
                Change(
                    scene_id=intent.scene_id,
                    kind="MOVE",
                    rule_id=RULE_EDIT_INTENT,
                    reason=f"AD edit: scene {intent.scene_id} moved {where}",
                    params={"action": "move_scene", "seq": seq, "ref": ref},
                )
            )

        elif intent.action == "swap_location":
            _require_pending_scene(order, work.scenes, intent.scene_id, seq)
            loc = intent.new_location_id
            if not loc or loc not in work.locations:
                raise EditError(f"Edit {seq}: unknown target location {loc!r}.")
            old = work.scenes[intent.scene_id].location_id
            work.scenes[intent.scene_id].location_id = loc
            changes.append(
                Change(
                    scene_id=intent.scene_id,
                    kind="MOVE",
                    rule_id=RULE_EDIT_INTENT,
                    reason=(
                        f"AD edit: scene {intent.scene_id} relocated "
                        f"{old} → {loc} ({work.locations[loc].name})"
                    ),
                    params={
                        "action": "swap_location",
                        "seq": seq,
                        "from": old,
                        "to": loc,
                    },
                )
            )

        elif intent.action == "add_scene":
            loc = intent.location_id
            if not loc or loc not in work.locations:
                raise EditError(f"Edit {seq}: unknown location {loc!r} for the new scene.")
            new_id = f"SC-E{seq:02d}"
            if new_id in work.scenes:
                raise EditError(f"Edit {seq}: generated scene id {new_id} already exists.")
            try:
                int_ext = IntExt(intent.int_ext.upper())
                day_night = DayNight(intent.day_night.upper())
            except ValueError as exc:
                raise EditError(
                    f"Edit {seq}: invalid INT/EXT or DAY/NIGHT value ({exc})."
                ) from exc
            scene = Scene(
                id=new_id,
                title=intent.title or "AD addition",
                page_count=float(intent.page_count),
                location_id=loc,
                int_ext=int_ext,
                day_night=day_night,
            )
            work.scenes[new_id] = scene
            work.scene_order.append(new_id)
            ref = intent.ref_scene_id
            if ref is not None:
                if ref not in order:
                    raise EditError(
                        f"Edit {seq}: reference scene {ref} is not in the remaining day."
                    )
                order.insert(order.index(ref) + 1, new_id)
            else:
                order.append(new_id)
            changes.append(
                Change(
                    scene_id=new_id,
                    kind="ADD",
                    rule_id=RULE_EDIT_INTENT,
                    reason=(
                        f"AD edit: added scene {new_id} · {scene.title} at {loc} "
                        f"({intent.page_count} pages)"
                    ),
                    params={
                        "action": "add_scene",
                        "seq": seq,
                        "pages": intent.page_count,
                        "location": loc,
                    },
                )
            )

    return replan(
        work,
        rbc,
        completed_scene_ids,
        incident
        or Incident(type="OTHER", free_text="AD schedule edit", source="manual_form"),
        now_minutes=now_minutes,
        plan_id=plan_id,
        created_at=created_at,
        strategy=strategy,
        seed_order=order,
        baseline_order_override=list(production.scene_order),
        extra_changes=changes,
    )
