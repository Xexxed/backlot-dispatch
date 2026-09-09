"""Cost & compliance ledger: pure pricing invariants + rate-card loading.

Invariants under test (plan §1–2):
  * a hold-strategy plan prices strictly more than minimal for the same
    incident;
  * a late lunch costs exactly headcount × ceil(late/30) × penalty;
  * hours exactly at the OT threshold produce zero OT; beyond it, OT at the
    tier multiplier;
  * determinism: same inputs → identical breakdown;
  * malformed rates.csv raises ImportValidationError with the row number;
  * absent rates.csv → load_rates None, app boots, pages unchanged.
"""
from __future__ import annotations

import pytest

from app.costs import RateCard, RateTier, price_plan
from app.engine import STRATEGIES, replan
from app.importers import ImportValidationError, load_rates
from app.models import Incident
from app.serialize import option_stats, slot_rows


INCIDENT = Incident(
    type="LOCATION_BLOCKED",
    location_id="L-STAGE4",
    blocked_until="14:00",
    severity="high",
    free_text="Generator down",
    source="manual_form",
)

RATECARD = RateCard(
    departments={"Camera": 65.0, "G&E": 55.0, "Sound": 70.0, "AD/Production": 45.0, "*": 60.0},
    ot_tiers={
        "Camera": [RateTier(10, 1.5), RateTier(12, 2.0)],
        "G&E": [RateTier(10, 1.5), RateTier(12, 2.0)],
        "Sound": [RateTier(10, 1.5), RateTier(12, 2.0)],
        "AD/Production": [RateTier(10, 1.5), RateTier(12, 2.0)],
        "*": [RateTier(10, 1.5), RateTier(12, 2.0)],
    },
    meal_penalty_per_person=25.0,
    company_move_cost=450.0,
)


def _plan_payload(production, rbc, strategy="minimal", *, meal_start=None):
    proposal = replan(
        production,
        rbc,
        completed_scene_ids=[],
        incident=INCIDENT,
        now_minutes=570,
        plan_id="t",
        created_at="2026-09-01T09:00:00+00:00",
        strategy=strategy,
    )
    payload = {
        "proposed_timeline": [s.__dict__ for s in proposal.proposed_timeline],
        "now_minutes": 570,
    }
    if meal_start is not None:
        rows = slot_rows(proposal.proposed_timeline)
        non_meal = [r for r in rows if r["item_id"] != "__MEAL__"]
        rebuilt = []
        clock = meal_start
        for r in non_meal:
            dur = r["end"] - r["start"]
            rebuilt.append({"item_id": r["item_id"], "start": clock, "end": clock + dur})
            clock += dur
        rebuilt.append({"item_id": "__MEAL__", "start": meal_start, "end": meal_start + 30})
        payload["proposed_timeline"] = rebuilt
    return payload


# ------------------------------------------------------------------ engine
def test_hold_prices_strictly_more_than_minimal(production, rbc):
    minimal = price_plan(_plan_payload(production, rbc, "minimal"), production, rbc, RATECARD)
    hold = price_plan(_plan_payload(production, rbc, "hold"), production, rbc, RATECARD)
    assert hold.total > minimal.total


def test_late_lunch_penalty_exact(production, rbc):
    headcount = len(production.crew) + len(production.cast)
    payload = _plan_payload(production, rbc, "minimal", meal_start=rbc.rulebook["meal_within_minutes"] + production.call_time + 60)
    cost = price_plan(payload, production, rbc, RATECARD)
    assert cost.penalty_meals == 2  # 60 min late → 2 half-hour units
    assert cost.penalty_people == headcount
    assert cost.meal_penalty_cost == 2 * headcount * 25.0


def test_ontime_lunch_no_penalty(production, rbc):
    payload = _plan_payload(production, rbc, "minimal", meal_start=production.call_time + 300)
    cost = price_plan(payload, production, rbc, RATECARD)
    assert cost.penalty_meals == 0
    assert cost.meal_penalty_cost == 0.0


def _tiny_production(departments=None, with_cast=True):
    """One-scene production with full control over spans (rows carry explicit
    start/end, so price_plan's math is testable without engine durations)."""
    from app.models import (
        CastMember,
        CrewMember,
        DayNight,
        IntExt,
        Location,
        Production,
        Scene,
    )

    scene = Scene(
        id="SC-1",
        title="Test",
        page_count=1.0,
        location_id="L-A",
        int_ext=IntExt.INT,
        day_night=DayNight.DAY,
        cast_ids=["C-1"] if with_cast else [],
        departments=departments if departments is not None else ["Camera"],
    )
    crew = [CrewMember(id="K-1", name="Kam", department="Camera", role="Operator A")]
    if not with_cast:
        crew = []  # cast/dept isolation: only the requested department bills
    return Production(
        id="P-T",
        title="T",
        shoot_date="2026-09-01",
        call_time=420,
        scenes={"SC-1": scene},
        scene_order=["SC-1"],
        crew=crew,
        cast={"C-1": CastMember(id="C-1", name="Maya Chen", character="Delia Harper")}
        if with_cast
        else {},
        locations={
            "L-A": Location(id="L-A", name="Stage A"),
            "L-B": Location(id="L-B", name="Ranch"),
            "L-C": Location(id="L-C", name="Main St"),
        },
    )


def test_ot_boundary_exact_at_threshold_is_zero():
    from app.rulebook import RuleBookContext, load_rulebook

    prod = _tiny_production(with_cast=False)  # isolate the Camera department
    rbc = RuleBookContext(load_rulebook())
    # Camera scene spans exactly 10h: straight time, no OT.
    payload = {
        "proposed_timeline": [
            {"item_id": "SC-1", "start": 420, "end": 1020,
             "location_id": "L-A", "label": "X"},
        ],
        "now_minutes": 420,
    }
    cost = price_plan(payload, prod, rbc, RATECARD)
    assert cost.ot_hours == 0.0
    assert cost.ot_cost == 0.0
    # One minute beyond the threshold bills OT at the tier multiplier.
    payload_late = {
        "proposed_timeline": [
            {"item_id": "SC-1", "start": 420, "end": 1021,
             "location_id": "L-A", "label": "X"},
        ],
        "now_minutes": 420,
    }
    cost_late = price_plan(payload_late, prod, rbc, RATECARD)
    assert cost_late.ot_hours == pytest.approx(1 / 60.0, rel=1e-6)
    assert cost_late.ot_cost == pytest.approx((1 / 60.0) * 65.0 * 1.5, rel=1e-6)


def test_second_ot_tier_bills_at_higher_multiplier():
    from app.rulebook import RuleBookContext, load_rulebook

    prod = _tiny_production(with_cast=False)  # isolate the Camera department
    rbc = RuleBookContext(load_rulebook())
    payload = {
        "proposed_timeline": [
            {"item_id": "SC-1", "start": 420, "end": 420 + 13 * 60,
             "location_id": "L-A", "label": "X"},
        ],
        "now_minutes": 420,
    }
    cost = price_plan(payload, prod, rbc, RATECARD)
    # hours 10-12 at 1.5, hours 12-13 at 2.0 (union-style band math).
    assert cost.ot_hours == pytest.approx(3.0)
    assert cost.ot_cost == pytest.approx(2 * 65.0 * 1.5 + 1 * 65.0 * 2.0)


def test_company_moves_counted_in_order():
    from app.models import Scene
    from app.rulebook import RuleBookContext, load_rulebook

    prod = _tiny_production()
    order = ["L-A", "L-B", "L-B", "L-C"]
    for i, loc in enumerate(order):
        prod.scenes[f"SC-{i}"] = Scene(
            id=f"SC-{i}", title=f"S{i}", page_count=1.0, location_id=loc,
            int_ext=prod.scenes["SC-1"].int_ext, day_night=prod.scenes["SC-1"].day_night,
            cast_ids=["C-1"], departments=["Camera"],
        )
    prod.scene_order = [f"SC-{i}" for i in range(len(order))]
    rbc = RuleBookContext(load_rulebook())
    payload = {
        "proposed_timeline": [
            {"item_id": f"SC-{i}", "start": 420 + i * 30, "end": 450 + i * 30,
             "location_id": loc, "label": loc}
            for i, loc in enumerate(order)
        ],
        "now_minutes": 420,
    }
    cost = price_plan(payload, prod, rbc, RATECARD)
    assert cost.company_moves == 2  # L-A→L-B, L-B→L-C (stay at L-B is not a move)
    assert cost.company_move_cost_total == 900.0


def test_determinism(production, rbc):
    payload = _plan_payload(production, rbc, "minimal")
    a = price_plan(payload, production, rbc, RATECARD)
    b = price_plan(payload, production, rbc, RATECARD)
    assert a.total == b.total
    assert a.lines == b.lines


def test_breakdown_sums_to_total(production, rbc):
    cost = price_plan(_plan_payload(production, rbc, "hold"), production, rbc, RATECARD)
    assert cost.total == pytest.approx(
        cost.straight_cost + cost.ot_cost + cost.meal_penalty_cost + cost.company_move_cost_total
    )


# ----------------------------------------------------------------- importer
def test_load_rates_from_seed(seed_dir=None):
    from pathlib import Path

    card = load_rates(Path(__file__).resolve().parents[1] / "seed")
    assert card is not None
    assert card.rate_for("Camera") == 65.0
    assert card.rate_for("Nonexistent Dept") == 60.0  # wildcard
    assert card.meal_penalty_per_person == 25.0
    assert card.company_move_cost == 450.0
    tiers = card.tiers_for("Sound")
    assert [t.multiplier for t in sorted(tiers, key=lambda t: t.threshold_hours)] == [1.5, 2.0]


def test_malformed_rates_raises_loudly_with_row(tmp_path):
    bad = tmp_path / "rates.csv"
    bad.write_text(
        "department,hourly_rate,ot_multiplier,ot_threshold_hours,meal_penalty_per_person,company_move_cost\n"
        "Camera,abc,1.5,10,25,450\n",
        encoding="utf-8",
    )
    with pytest.raises(ImportValidationError) as exc:
        load_rates(tmp_path)
    assert "rates.csv:2" in str(exc.value)  # header is line 1, data row is 2


def test_missing_wildcard_rejected(tmp_path):
    bad = tmp_path / "rates.csv"
    bad.write_text(
        "department,hourly_rate,ot_multiplier,ot_threshold_hours,meal_penalty_per_person,company_move_cost\n"
        "Camera,65,1.5,10,25,450\n",
        encoding="utf-8",
    )
    with pytest.raises(ImportValidationError, match="wildcard"):
        load_rates(tmp_path)


def test_conflicting_rate_rejected(tmp_path):
    bad = tmp_path / "rates.csv"
    bad.write_text(
        "department,hourly_rate,ot_multiplier,ot_threshold_hours,meal_penalty_per_person,company_move_cost\n"
        "*,60,1.5,10,25,450\n"
        "Camera,65,1.5,10,25,450\n"
        "Camera,90,1.5,10,25,450\n",
        encoding="utf-8",
    )
    with pytest.raises(ImportValidationError, match="conflicting"):
        load_rates(tmp_path)


def test_absent_rates_file_returns_none(tmp_path):
    assert load_rates(tmp_path) is None


# ------------------------------------------------------------------ surfaces
def test_sandbox_shows_cost_rows_with_rates(client):
    """With seed/rates.csv present, every sandbox card carries a dollar
    exposure and the hold anchor."""
    resp = client.post(
        "/incident",
        data={
            "force_manual": "1",
            "manual_type": "LOCATION_BLOCKED",
            "manual_location": "L-STAGE4",
            "manual_blocked_until": "14:00",
            "free_text": "Generator down",
            "now_override": "11:40",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    gid = resp.headers["location"].rsplit("/", 1)[-1]
    page = client.get(f"/sandbox/{gid}").text
    assert "Cost exposure" in page
    assert "vs hold" in page
    assert "illustrative demo rates" in page
    st = client.app.state
    plans = st.store.plans_in_group(gid)
    stats = {p["strategy"]: option_stats(p, _price(p, st)) for p in plans}
    assert stats["hold"]["cost_total"] > stats["minimal"]["cost_total"]
    assert (
        stats["minimal"]["cost_total"] - stats["hold"]["cost_total"] < 0
    )  # minimal saves vs hold


def _price(plan, st):
    from app.costs import price_plan

    return price_plan(plan, st.production, st.rbc, st.rates)


def test_plan_diff_shows_breakdown_and_print_button(client):
    resp = client.post(
        "/incident",
        data={
            "force_manual": "1",
            "manual_type": "LOCATION_BLOCKED",
            "manual_location": "L-STAGE4",
            "manual_blocked_until": "14:00",
            "free_text": "Generator down",
            "now_override": "11:40",
        },
        follow_redirects=False,
    )
    gid = resp.headers["location"].rsplit("/", 1)[-1]
    plans = client.app.state.store.plans_in_group(gid)
    page = client.get(f"/plans/{plans[0]['id']}").text
    assert "Cost exposure" in page
    assert "Crew hours" in page
    assert "Print call sheet" in page
    assert "Total exposure" in page


def test_dashboard_banner_after_publish(client):
    """Publish the minimal option of a group: the dashboard banner states the
    exposure vs Hold & wait with a positive 'Avoided' figure."""
    resp = client.post(
        "/incident",
        data={
            "force_manual": "1",
            "manual_type": "LOCATION_BLOCKED",
            "manual_location": "L-STAGE4",
            "manual_blocked_until": "14:00",
            "free_text": "Generator down",
            "now_override": "11:40",
        },
        follow_redirects=False,
    )
    gid = resp.headers["location"].rsplit("/", 1)[-1]
    plans = client.app.state.store.plans_in_group(gid)
    minimal = next(p for p in plans if p["strategy"] == "minimal")
    client.post(f"/plans/{minimal['id']}/select", follow_redirects=False)
    client.post(f"/plans/{minimal['id']}/publish", follow_redirects=False)
    dash = client.get("/").text
    assert "Option exposure:" in dash
    assert "vs Hold &amp; wait:" in dash
    assert "Avoided $" in dash


def test_no_rates_pages_render_unchanged(client, monkeypatch):
    """With the ledger off (rates None), the same pages render with NO cost
    rows, banner, or print banner — and option_stats stays None-safe."""
    st = client.app.state
    monkeypatch.setattr(st, "rates", None)
    resp = client.post(
        "/incident",
        data={
            "force_manual": "1",
            "manual_type": "LOCATION_BLOCKED",
            "manual_location": "L-STAGE4",
            "manual_blocked_until": "14:00",
            "free_text": "Generator down",
            "now_override": "11:40",
        },
        follow_redirects=False,
    )
    gid = resp.headers["location"].rsplit("/", 1)[-1]
    page = client.get(f"/sandbox/{gid}").text
    assert "Cost exposure" not in page
    assert "illustrative demo rates" not in page
    plans = st.store.plans_in_group(gid)
    minimal = next(p for p in plans if p["strategy"] == "minimal")
    diff_page = client.get(f"/plans/{minimal['id']}").text
    assert "Cost exposure" not in diff_page
    assert "Print call sheet" in diff_page  # button harmless without rates
    client.post(f"/plans/{minimal['id']}/publish", follow_redirects=False)
    dash = client.get("/").text
    assert "Option exposure:" not in dash
    stats = option_stats(minimal)
    assert stats["cost_total"] is None and stats["penalty_meals"] is None
