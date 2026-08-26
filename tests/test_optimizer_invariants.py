"""Optimizer invariant tests.

Core contract: a proposal is feasible IF AND ONLY IF the independent validator
finds zero hard-rule violations on its proposed items. Plus targeted tests per
rule, determinism, and loud infeasibility.
"""
from __future__ import annotations

import random

import pytest

from app.engine import make_context, replan
from app.models import MEAL_ID, Incident
from app.rules import validate_order
from app.timeline import BlockedWindow


def run(production, rbc, incident, now=540, completed=None, plan_id="t"):
    return replan(
        production,
        rbc,
        completed_scene_ids=completed or [],
        incident=incident,
        now_minutes=now,
        plan_id=plan_id,
        created_at="2026-09-01T09:00:00+00:00",
    )


def rebuild_ctx(production, rbc, proposal):
    """Reconstruct the engine's PlanContext independently from the proposal."""
    incident = Incident(**proposal.incident)
    windows: list[BlockedWindow] = []
    if incident.location_id:
        until = incident.blocked_until_minutes()
        if until is not None:
            windows.append(
                BlockedWindow(
                    incident.location_id, min(proposal.now_minutes, until), until
                )
            )
    return make_context(
        production,
        rbc,
        earliest_start=max(proposal.now_minutes, production.call_time),
        blocked_windows=windows,
    ), incident


def items_of(proposal):
    k = proposal.meal_after_index
    return proposal.proposed_order[:k] + [MEAL_ID] + proposal.proposed_order[k:]


def violations_of(production, rbc, proposal):
    ctx, _ = rebuild_ctx(production, rbc, proposal)
    return validate_order(items_of(proposal), ctx)


# ------------------------------------------------------------------ contract
DEMO_INCIDENT = Incident(
    type="LOCATION_BLOCKED",
    location_id="L-STAGE4",
    blocked_until="14:00",
    severity="high",
    free_text="Generator down at Stage 4",
    confidence=1.0,
    source="manual_form",
)


@pytest.mark.parametrize("seed", range(24))
def test_feasible_iff_zero_violations(production, rbc, seed):
    rnd = random.Random(seed)
    if seed % 4 == 3:
        incident = Incident(type="OTHER", free_text="misc chaos")
    else:
        location = rnd.choice(["L-STAGE4", "L-RANCH", "L-MAINST"])
        now = rnd.randint(420, 720)
        until_hhmm = f"{min((now + rnd.randint(30, 300)) // 60, 23):02d}:{rnd.choice(['00','15','30','45'])}"
        incident = Incident(
            type="LOCATION_BLOCKED",
            location_id=location,
            blocked_until=until_hhmm,
            severity="high",
            source="manual_form",
        )
    completed_n = rnd.randint(0, 8)
    p = run(production, rbc, incident, now=rnd.randint(420, 660),
            completed=production.scene_order[:completed_n])
    violations = violations_of(production, rbc, p)
    assert p.is_feasible == (violations == []), (
        f"seed={seed}: feasible={p.is_feasible} but violations={violations}"
    )
    if not p.is_feasible:
        assert p.diagnostics, "infeasible proposals must carry explicit diagnostics"


def test_dependencies_preserved_in_all_scenarios(production, rbc):
    for now in (480, 600, 780):
        p = run(production, rbc, DEMO_INCIDENT, now=now, completed=["SC-101", "SC-102"])
        position = {sid: i for i, sid in enumerate(p.proposed_order)}
        for sid in p.proposed_order:
            for dep in production.scenes[sid].depends_on:
                assert position[dep] < position[sid]


def test_blocked_location_respected_when_feasible(production, rbc):
    p = run(production, rbc, DEMO_INCIDENT, now=570, completed=production.scene_order[:5])
    assert p.is_feasible
    stage_scenes = [
        s
        for s in p.proposed_timeline
        if s.location_id == "L-STAGE4"
    ]
    assert stage_scenes and all(s.start >= 14 * 60 for s in stage_scenes)
    assert any(c.rule_id == "R-BLOCKED-LOCATION" for c in p.changes)


def test_daylight_respected_after_block_pushes_ext_late(production, rbc):
    # Block the ranch all afternoon: EXT/DAY scenes there must end up either
    # before sunset or flagged — never silently outside daylight.
    incident = Incident(
        type="LOCATION_BLOCKED",
        location_id="L-RANCH",
        blocked_until="18:00",
        severity="high",
        source="manual_form",
    )
    p = run(production, rbc, incident, now=480)
    if p.is_feasible:
        sunset = rbc.sunset
        for slot in p.proposed_timeline:
            scene = production.scenes[slot.item_id]
            if scene.int_ext.value == "EXT" and scene.day_night.value == "DAY":
                assert slot.start >= rbc.sunrise and slot.end <= sunset


def test_meal_within_window_when_feasible(production, rbc):
    p = run(production, rbc, DEMO_INCIDENT, now=540)
    deadline = production.call_time + int(rbc.rulebook["meal_within_minutes"])
    meals = [s for s in p.proposed_timeline if s.is_meal]
    assert len(meals) == 1
    assert meals[0].start <= deadline


def test_compliant_incident_changes_nothing(production, rbc):
    incident = Incident(type="WEATHER", severity="low", free_text="drizzle passed")
    p = run(production, rbc, incident, now=450)
    assert p.is_feasible
    assert p.proposed_order == list(p.baseline_order)
    assert p.changes == []
    assert p.diagnostics == []


def test_completed_scenes_excluded(production, rbc):
    done = production.scene_order[:6]
    p = run(production, rbc, DEMO_INCIDENT, now=600, completed=done)
    assert set(p.completed_scene_ids) == set(done)
    assert not (set(p.proposed_order) & set(done))
    assert set(p.proposed_order) | set(done) == set(production.scene_order)


def test_determinism_same_inputs_same_plan(production, rbc):
    kwargs = dict(now=570, completed=production.scene_order[:5], plan_id="same")
    a = run(production, rbc, DEMO_INCIDENT, **kwargs)
    b = run(production, rbc, DEMO_INCIDENT, **kwargs)
    assert a.proposed_order == b.proposed_order
    assert a.changes == b.changes
    assert a.diagnostics == b.diagnostics
    assert a.meal_after_index == b.meal_after_index
    assert a.proposed_timeline == b.proposed_timeline
    assert a.baseline_timeline == b.baseline_timeline


def test_infeasible_is_loud_not_silent(production, rbc):
    # Block the ranch past sunset: late EXT/DAY ranch scenes cannot be legal.
    incident = Incident(
        type="LOCATION_BLOCKED",
        location_id="L-RANCH",
        blocked_until="23:50",
        severity="high",
        source="manual_form",
    )
    p = run(production, rbc, incident, now=750)  # 12:30
    assert not p.is_feasible
    assert any(d.severity == "ERROR" for d in p.diagnostics)
    # and the validator agrees with the diagnostics
    assert violations_of(production, rbc, p), "diagnostics must match reality"


def test_change_reasons_carry_rule_ids_and_params(production, rbc):
    p = run(production, rbc, DEMO_INCIDENT, now=570, completed=production.scene_order[:5])
    moves = [c for c in p.changes if c.kind == "MOVE"]
    assert moves, "demo disruption must produce at least one MOVE"
    for c in moves:
        assert c.rule_id.startswith("R-")
        assert c.reason and isinstance(c.params, dict)
