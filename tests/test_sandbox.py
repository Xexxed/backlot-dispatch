"""What-if sandbox: strategy registry, distinct deterministic options, store groups.

Covers the engine-side contract of Phase 8a: the three recovery postures are
distinct, deterministic, dependency-legal, and round-trip through the store.
"""
from __future__ import annotations

import pytest

from app.engine import STRATEGIES, replan
from app.models import Incident
from app.rules import check_dependencies


INCIDENT = Incident(
    type="LOCATION_BLOCKED",
    location_id="L-STAGE4",
    blocked_until="14:00",
    severity="high",
    free_text="Generator down",
    source="manual_form",
)


def make(production, rbc, strategy, **kw):
    return replan(
        production,
        rbc,
        completed_scene_ids=kw.pop("completed", []),
        incident=kw.pop("incident", INCIDENT),
        now_minutes=kw.pop("now", 570),
        plan_id=kw.pop("plan_id", "t"),
        created_at=kw.pop("created_at", "2026-09-01T09:00:00+00:00"),
        strategy=strategy,
        **kw,
    )


def test_registry_has_three_named_strategies():
    assert set(STRATEGIES) == {"minimal", "cover_set", "hold"}
    names = {s.name for s in STRATEGIES.values()}
    assert len(names) == 3  # each strategy is visibly distinct to a user


def test_unknown_strategy_raises():
    with pytest.raises(ValueError):
        replan(None, None, [], INCIDENT, 570, "a", "t", strategy="nope")  # type: ignore[arg-type]


def test_minimal_is_default_and_matches_explicit(production, rbc):
    a = replan(
        production, rbc, [], INCIDENT, 570, "a", "2026-09-01T09:00:00+00:00"
    )
    b = replan(
        production,
        rbc,
        [],
        INCIDENT,
        570,
        "b",
        "2026-09-01T09:00:00+00:00",
        strategy="minimal",
    )
    assert a.proposed_order == b.proposed_order
    assert a.strategy == "minimal"
    assert a.group_id == ""


def test_strategies_differ_and_are_deterministic(production, rbc):
    results = {sid: make(production, rbc, sid) for sid in STRATEGIES}
    for sid, p in results.items():
        again = make(production, rbc, sid)
        assert again.proposed_order == p.proposed_order
    orders = {tuple(p.proposed_order) for p in results.values()}
    assert len(orders) >= 2


def test_dependency_order_holds_across_all_strategies(production, rbc):
    for sid in STRATEGIES:
        p = make(production, rbc, sid)
        assert check_dependencies(p.proposed_order, production) == []


def test_minimal_opens_with_unblocked_unit(production, rbc):
    p = make(production, rbc, "minimal")
    # Minimal change never opens the shooting day by resuming the stalled unit;
    # the unblocked work stays first (unlike cover-set, which leads with it).
    assert production.scenes[p.proposed_order[0]].location_id != "L-STAGE4"
    assert any(c.rule_id == "R-BLOCKED-LOCATION" for c in p.changes)


def test_cover_set_resumes_blocked_unit_immediately(production, rbc):
    p = make(production, rbc, "cover_set")
    affected = [s for s in p.proposed_timeline if s.location_id == "L-STAGE4"]
    assert affected
    earliest = min(s.start for s in affected)
    assert earliest == 840  # 14:00, the moment the block clears
    assert production.scenes[p.proposed_order[0]].location_id == "L-STAGE4"
    assert any(c.rule_id == "R-COVER-SET" for c in p.changes)


def test_hold_preserves_original_order_and_shows_cost(production, rbc):
    p = make(production, rbc, "hold")
    assert p.proposed_order == production.scene_order
    assert not any(c.kind == "MOVE" for c in p.changes)
    minimal = make(production, rbc, "minimal")
    hold_wrap = max(s.end for s in p.proposed_timeline)
    min_wrap = max(s.end for s in minimal.proposed_timeline)
    assert hold_wrap >= min_wrap


def test_store_group_roundtrip(tmp_path):
    from app.store import Store

    store = Store(tmp_path / "g.db")
    group_id = "grp1"
    for i, sid in enumerate(STRATEGIES):
        store.save_plan(
            {
                "id": f"p{i}",
                "created_at": "2026-09-01T09:00:00+00:00",
                "incident": {},
                "group_id": group_id,
                "strategy": sid,
                "status": "proposed",
                "baseline_timeline": [],
                "proposed_timeline": [],
                "changes": [],
                "diagnostics": [],
                "is_feasible": True,
            }
        )
    group = store.plans_in_group(group_id)
    assert [p["strategy"] for p in group] == list(STRATEGIES)
    assert store.group_has_published(group_id) is False
    store.set_plan_status("p0", "published")
    assert store.group_has_published(group_id) is True
    store.close()


def test_store_migrates_legacy_schema(tmp_path):
    import sqlite3

    from app.store import Store

    db = tmp_path / "legacy.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE plans (id TEXT PRIMARY KEY, created_at TEXT, status TEXT, "
        "incident_json TEXT, payload_json TEXT)"
    )
    conn.execute(
        "INSERT INTO plans VALUES ('x','2026-09-01T09:00:00+00:00','proposed','{}','{}')"
    )
    conn.commit()
    conn.close()
    store = Store(db)
    assert store.plans_in_group("nope") == []
    row = store.get_plan("x")
    assert row is not None and row["group_id"] == ""
    store.close()
