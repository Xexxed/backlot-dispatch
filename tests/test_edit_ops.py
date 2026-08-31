"""NL schedule customization: structured edit intents applied deterministically.

The LLM (or manual form) only ever produces EditIntent objects; this suite
pins the contract that app/edit_ops.py is the only mutation point, every edit
lands in the machine-checkable change log, invalid intents fail LOUDLY, and
the edited proposal still passes dependency validation.
"""
from __future__ import annotations

import pytest

from app.agents.editor import manual_edit_intent
from app.edit_ops import EditError, apply_edits
from app.models import EditIntent
from app.rules import check_dependencies


def make(production, rbc, edits, **kw):
    return apply_edits(
        production,
        rbc,
        completed_scene_ids=kw.pop("completed", []),
        edits=edits,
        now_minutes=kw.pop("now", 570),
        plan_id=kw.pop("plan_id", "t"),
        created_at=kw.pop("created_at", "2026-09-01T09:00:00+00:00"),
        **kw,
    )


def test_move_scene_logs_intent_and_stays_legal(production, rbc):
    sid = production.scene_order[5]
    p = make(production, rbc, [EditIntent(action="move_scene", scene_id=sid)])
    assert any(
        c.rule_id == "R-EDIT-INTENT" and c.scene_id == sid and c.params["action"] == "move_scene"
        for c in p.changes
    )
    assert p.proposed_order.count(sid) == 1
    assert check_dependencies(p.proposed_order, production) == []


def test_move_after_reference_places_scene_there(production, rbc):
    sid, ref = production.scene_order[6], production.scene_order[1]
    p = make(
        production,
        rbc,
        [EditIntent(action="move_scene", scene_id=sid, ref_scene_id=ref)],
    )
    change = next(c for c in p.changes if c.params.get("action") == "move_scene")
    assert change.params["ref"] == ref


def test_swap_location_relocates_scene(production, rbc):
    sid = production.scene_order[3]
    current = production.scenes[sid].location_id
    other = next(l for l in production.locations if l != current)
    p = make(
        production,
        rbc,
        [EditIntent(action="swap_location", scene_id=sid, new_location_id=other)],
    )
    slot = next(s for s in p.proposed_timeline if s.item_id == sid)
    assert slot.location_id == other
    change = next(c for c in p.changes if c.params.get("action") == "swap_location")
    assert change.params == {
        "action": "swap_location",
        "seq": 1,
        "from": current,
        "to": other,
    }


def test_add_scene_appends_with_rulebook_duration(production, rbc):
    loc = next(iter(production.locations))
    p = make(
        production,
        rbc,
        [
            EditIntent(
                action="add_scene",
                title="Pickup shot",
                page_count=2.0,
                location_id=loc,
            )
        ],
    )
    assert "SC-E01" in p.proposed_order
    change = next(c for c in p.changes if c.kind == "ADD")
    assert change.scene_id == "SC-E01"
    slot = next(s for s in p.proposed_timeline if s.item_id == "SC-E01")
    assert (slot.end - slot.start) == 2 + 10  # 2 pages + staging overhead


def test_invalid_intents_fail_loudly(production, rbc):
    with pytest.raises(EditError):
        make(production, rbc, [])  # nothing requested
    with pytest.raises(EditError):
        make(
            production,
            rbc,
            [EditIntent(action="move_scene", scene_id="SC-999")],
        )
    with pytest.raises(EditError):
        make(
            production,
            rbc,
            [
                EditIntent(
                    action="swap_location",
                    scene_id=production.scene_order[0],
                    new_location_id="L-NOWHERE",
                )
            ],
        )
    with pytest.raises(EditError):
        make(production, rbc, [EditIntent(action="teleport_scene")])


def test_edits_are_deterministic(production, rbc):
    edits = [
        EditIntent(action="move_scene", scene_id=production.scene_order[5]),
        EditIntent(
            action="add_scene",
            title="Insert",
            page_count=1.0,
            location_id=next(iter(production.locations)),
        ),
    ]
    a = make(production, rbc, edits, plan_id="a")
    b = make(production, rbc, edits, plan_id="b")
    assert a.proposed_order == b.proposed_order
    assert [c.reason for c in a.changes] == [c.reason for c in b.changes]


def test_manual_intent_matches_gemini_contract(production, rbc):
    """The fallback form builds the same EditIntent object the LLM emits —
    one deterministic application path for both sources."""
    sid = production.scene_order[4]
    intent = manual_edit_intent(action="move_scene", scene_id=sid)
    assert intent.source == "manual_form" and intent.confidence == 1.0
    p = make(production, rbc, [intent])
    assert any(
        c.rule_id == "R-EDIT-INTENT" and c.scene_id == sid for c in p.changes
    )


def test_edit_never_mutates_shared_production(production, rbc):
    sid = production.scene_order[3]
    before = production.scenes[sid].location_id
    other = next(l for l in production.locations if l != before)
    make(
        production,
        rbc,
        [EditIntent(action="swap_location", scene_id=sid, new_location_id=other)],
    )
    assert production.scenes[sid].location_id == before
    assert all(not s.startswith("SC-E") for s in production.scene_order)


def test_edit_flow_end_to_end(client):
    production = client.app.state.production
    sid = production.scene_order[5]
    r = client.post(
        "/edit",
        data={
            "force_manual": "1",
            "manual_action": "move_scene",
            "manual_scene": sid,
            "manual_ref_scene": "",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    assert r.headers["location"].startswith("/plans/")
    diff = client.get(r.headers["location"])
    assert diff.status_code == 200
    assert "R-EDIT-INTENT" in diff.text


def test_edit_falls_back_when_gemini_unconfigured(client):
    r = client.post(
        "/edit",
        data={"free_text": "Move SC-118 to the end of the day"},
        follow_redirects=False,
    )
    assert r.status_code == 200
    assert "handed off" in r.text  # editor agent fallback banner
