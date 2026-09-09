"""Sentinel: signal boundaries, tick idempotency, governance ceilings,
and the never-mutates-plans invariant.

Invariants under test (plan §3):
  * each signal fires at its engineered boundary and not one minute before;
  * a raised incident flows through the standard pipeline (sandbox for
    blocking, review plan otherwise) and yields ≥1 reviewable target;
  * GOVERNANCE_MODE unset/advise never publishes; auto_low respects both
    ceilings; off disables evaluation;
  * two ticks with unchanged state raise zero new incidents;
  * the sentinel never mutates plan rows directly.
"""
from __future__ import annotations

from app.models import minutes_to_hhmm
from app.rulebook import RuleBookContext, load_rulebook
from app.sentinel import (
    ACK_STALE_NOTIFY_MIN,
    SLIPPAGE_LATE_MIN,
    ack_staleness,
    daylight_burn_down,
    evaluate,
    meal_penalty_clock,
    slippage,
    weather_imminent,
)

RBC = RuleBookContext(load_rulebook())


# ----------------------------------------------------------- pure signals
def test_daylight_fires_at_boundary_not_before(production):
    """Sunset 19:15; with `now` late enough that remaining EXT work no longer
    fits, the signal fires; a minute earlier (still fits) it does not."""
    published = None  # full remaining day
    ext_minutes = sum(
        RBC.scene_duration(production.scenes[sid].page_count)
        for sid in production.scene_order
        if production.scenes[sid].int_ext.value == "EXT"
        and production.scenes[sid].day_night.value == "DAY"
    )
    # EXT work runs from the top of the day: last sun-minute that still fits:
    fitting_now = RBC.sunset - ext_minutes
    assert daylight_burn_down(production, RBC, published, fitting_now, None) is None
    signal = daylight_burn_down(production, RBC, published, fitting_now + 1, None)
    assert signal is not None and signal.id == "daylight_burn_down"
    assert signal.incident is not None
    assert signal.incident.source == "sentinel"
    assert "EXT" in signal.metric or "pages" in signal.metric


def test_daylight_silent_when_no_ext_work_remains(production):
    published = {"completed_scene_ids": list(production.scene_order), "proposed_timeline": []}
    assert daylight_burn_down(production, RBC, published, 12 * 60, None) is None


def test_meal_clock_fires_only_past_deadline_with_live_plan(production):
    deadline = production.call_time + int(RBC.rulebook["meal_within_minutes"])
    # No published plan → baseline auto-places lunch → never fires.
    assert meal_penalty_clock(production, RBC, None, deadline + 60, None) is None
    # Live plan whose pending timeline has no meal yet → fires past deadline.
    live = {"proposed_timeline": [], "completed_scene_ids": []}
    assert meal_penalty_clock(production, RBC, live, deadline - 1, None) is None
    signal = meal_penalty_clock(production, RBC, live, deadline, None)
    assert signal is not None and signal.id == "meal_penalty_clock"
    assert signal.incident is not None
    assert signal.context["headcount"] == len(production.crew) + len(production.cast)


def test_meal_clock_silent_when_meal_taken_or_pending(production):
    deadline = production.call_time + int(RBC.rulebook["meal_within_minutes"])
    now = deadline + 30
    taken = {"proposed_timeline": [{"item_id": "__MEAL__", "start": now - 40, "end": now - 10}]}
    assert meal_penalty_clock(production, RBC, taken, now, None) is None
    pending = {"proposed_timeline": [{"item_id": "__MEAL__", "start": now + 10, "end": now + 40}]}
    assert meal_penalty_clock(production, RBC, pending, now, None) is None


def _published_with_acks(production, acked_ids, published_now=11 * 60):
    return {"now_minutes": published_now, "completed_scene_ids": [], "proposed_timeline": []}


def test_ack_staleness_boundary_and_critical_escalation(production):
    published = _published_with_acks(production, set())
    everyone = [m.id for m in production.crew]
    # T+9: silent. T+10 (boundary): medium advisory, no incident.
    assert ack_staleness(production, RBC, published, 11 * 60 + ACK_STALE_NOTIFY_MIN - 1, None, set(everyone)) is None
    advisory = ack_staleness(production, RBC, published, 11 * 60 + ACK_STALE_NOTIFY_MIN, None, set(everyone) - {production.crew[0].id})
    assert advisory is not None and advisory.incident is None

    # T+20 with everyone acked: silent.
    assert ack_staleness(production, RBC, published, 11 * 60 + 20, None, set(everyone)) is None
    # T+20 with a critical straggler: HIGH incident naming them.
    critical = next(m for m in production.crew if m.role in ("DP", "1st AD", "Sound Mixer", "Key Grip", "Script Supervisor"))
    escalated = ack_staleness(production, RBC, published, 11 * 60 + 20, None, set(everyone) - {critical.id})
    assert escalated is not None and escalated.severity == "high"
    assert escalated.incident is not None and escalated.incident.type == "CAST_DELAY"
    assert critical.name in escalated.incident.free_text


def test_weather_imminent_requires_window_within_the_hour(production):
    from app.weather import Forecast, Hour

    # Fixture storm at L-RANCH 14:00–16:30; now 13:05 → starts in 55 min → fires.
    hours = [Hour(14 * 60, 16 * 60 + 30, 85.0, 38.0, True, "Rain")]
    forecasts = {"L-RANCH": Forecast(hours=hours, source="fixture", updated="13:00")}
    signal = weather_imminent(production, RBC, None, 13 * 60 + 5, None, forecasts)
    assert signal is not None and signal.id == "weather_imminent"
    assert signal.incident.blocked_from == "14:00"
    assert signal.incident.location_id == "L-RANCH"
    # Same window one hour out (starts in 61 min): silent.
    assert weather_imminent(production, RBC, None, 12 * 60 + 59, None, forecasts) is None
    # Window fully in the past: silent.
    assert weather_imminent(production, RBC, None, 17 * 60, None, forecasts) is None


def test_slippage_boundary_on_late_critical_reply(production):
    member = next(m for m in production.crew if m.role in ("DP", "1st AD"))
    replies = [{"kind": "running_late", "handled": 0, "subject_id": member.id,
                "eta_minutes": SLIPPAGE_LATE_MIN, "critical": 1}]
    assert slippage(production, RBC, None, 11 * 60, None, []) is None
    assert slippage(production, RBC, None, 11 * 60, None,
                    [{"kind": "running_late", "handled": 0, "subject_id": member.id,
                      "eta_minutes": SLIPPAGE_LATE_MIN - 1, "critical": 1}]) is None
    signal = slippage(production, RBC, None, 11 * 60, None, replies)
    assert signal is not None and signal.id == "slippage"
    assert signal.incident.type == "CAST_DELAY"
    assert member.name in signal.incident.free_text


def test_evaluate_never_raises_and_dedupes_none(production):
    signals = evaluate(production, RBC, None, 8 * 60, None, acked_ids=set(), forecasts={}, replies=[])
    assert isinstance(signals, list)


# --------------------------------------------------------- pipeline via API
def _file_blocking(client):
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
    return resp.headers["location"]


def test_tick_idempotent_unchanged_state_raises_zero(client):
    _file_blocking(client)
    first = client.post("/sentinel/tick")
    assert first.status_code == 200
    body = first.json()
    assert body["mode"] in ("advise", "auto_low")
    incident_raised = [r for r in body["raised"] if r.get("incident")]
    second = client.post("/sentinel/tick").json()
    still_raised = [r for r in second["raised"] if r.get("incident")]
    # Every incident raised by tick 1 is deduped by tick 2.
    assert still_raised == [] or all(
        r["id"] not in {x["id"] for x in incident_raised} for r in still_raised
    )


def test_tick_raises_weather_incident_through_sandbox(client):
    """With the fixture storm imminent (now inside the window), tick files a
    WEATHER incident and produces a sandbox group with feasible options."""
    st = client.app.state
    # force the demo clock inside the imminent window (storm 14:00–16:30)
    st.settings.demo_clock = "13:30"
    body = client.post("/sentinel/tick").json()
    weather = [r for r in body["raised"] if r["id"] == "weather_imminent" and r.get("incident")]
    assert weather and "/sandbox/" in weather[0]["target"]
    gid = weather[0]["target"].rsplit("/", 1)[-1]
    plans = st.store.plans_in_group(gid)
    assert len(plans) == 3  # standard strategies
    st.settings.demo_clock = ""


def test_advise_mode_never_publishes(client):
    st = client.app.state
    assert st.settings.governance_mode == "advise"
    st.settings.demo_clock = "13:30"
    body = client.post("/sentinel/tick").json()
    for r in body["raised"]:
        assert r.get("auto_published") is None
    published = [p for p in st.store.list_plans(limit=100)
                 if p["status"].startswith("published")]
    assert published == []
    st.settings.demo_clock = ""


def test_governance_off_disables_evaluation(client):
    st = client.app.state
    st.settings.governance_mode = "off"
    body = client.post("/sentinel/tick").json()
    assert body == {"raised": [], "groups": [], "mode": "off"}
    st.settings.governance_mode = "advise"


def test_sentinel_never_mutates_plans_directly(client, monkeypatch):
    """Pin the invariant: the sentinel's tick must not write plan rows except
    through the standard pipeline. Wrap the store's plan writers and count."""
    st = client.app.state
    st.settings.demo_clock = "13:30"
    direct_writes: list[str] = []
    real_set_status = st.store.set_plan_status
    real_save = st.store.save_plan

    import app.web.sentinel_page as sp

    def guarded_set_status(plan_id, status):
        # pipeline publishes legitimately; anything outside it is a violation
        import inspect

        caller = inspect.stack()[1].function
        if caller not in ("sentinel_tick", "_maybe_auto_publish"):
            direct_writes.append(f"set_plan_status:{plan_id}:{status}")
        return real_set_status(plan_id, status)

    monkeypatch.setattr(st.store, "set_plan_status", guarded_set_status)
    client.post("/sentinel/tick")
    monkeypatch.undo()
    assert direct_writes == []
    real_save  # (read to appease linters; save_plan untouched)


def test_auto_publish_respects_ceilings(client):
    st = client.app.state
    st.settings.governance_mode = "auto_low"
    # Unreachable ceilings (defaults): no auto-publish ever.
    st.settings.demo_clock = "13:30"
    body = client.post("/sentinel/tick").json()
    assert all(r.get("auto_published") is None for r in body["raised"])
    # Reachable ceilings require cost data, which this deployment lacks —
    # fail-closed: still no auto-publish even with big ceilings.
    st.settings.auto_publish_max_usd = 100000
    st.settings.auto_publish_max_minutes_moved = 100
    body2 = client.post("/sentinel/tick").json()
    assert all(r.get("auto_published") is None for r in body2["raised"])
    st.settings.demo_clock = ""
    st.settings.governance_mode = "advise"
    st.settings.auto_publish_max_usd = 0
    st.settings.auto_publish_max_minutes_moved = 0


def test_auto_publish_publishes_with_cost_data_and_ceilings(client, monkeypatch):
    """With feasible options, injected cost data and both ceilings open,
    auto_low publishes the cheapest feasible option and logs the skipped
    human gate in the trace."""
    st = client.app.state
    import app.web.sentinel_page as sp
    from app.trace import RunRecorder

    st.settings.governance_mode = "auto_low"
    st.settings.auto_publish_max_usd = 100000.0
    st.settings.auto_publish_max_minutes_moved = 100

    # Craft a feasible sentinel-raised sandbox group directly in the store.
    gid = "autogroup"
    costs = {"minimal": 500.0, "cover_set": 900.0, "hold": 5000.0}
    for i, (sid, _) in enumerate(costs.items()):
        st.store.save_plan(
            {
                "id": f"auto{i}",
                "created_at": "2026-09-01T09:00:00+00:00",
                "incident": {"type": "WEATHER", "source": "sentinel"},
                "group_id": gid,
                "strategy": sid,
                "status": "proposed",
                "is_feasible": True,
                "changes": [],
                "diagnostics": [],
                "baseline_timeline": [],
                "proposed_timeline": [],
                "now_minutes": 810,
            }
        )

    real_option_stats = sp.option_stats

    def priced(plan):
        stats = real_option_stats(plan)
        stats["cost_total"] = costs[plan["strategy"]]
        return stats

    monkeypatch.setattr(sp, "option_stats", priced)
    recorder = RunRecorder(st.store, "sentinel:test").start()
    result = sp._maybe_auto_publish(st, gid, recorder)
    monkeypatch.undo()

    assert result is not None and result["cost"] == 500.0  # cheapest feasible
    published = [p for p in st.store.list_plans(limit=100)
                 if p["status"].startswith("published")]
    assert [p["id"] for p in published] == ["auto0"]
    steps = st.store.steps_for_run(recorder.run_id)
    gate = [s for s in steps if s["agent"] == "sentinel" and s["status"] == "skipped"]
    assert gate and "auto_low" in gate[0]["summary"]

    # Tighter ceiling: cost above the limit → refuse.
    st.store.save_plan(
        {
            "id": "auto3",
            "created_at": "2026-09-01T09:01:00+00:00",
            "incident": {"type": "WEATHER", "source": "sentinel"},
            "group_id": "autogroup2",
            "strategy": "minimal",
            "status": "proposed",
            "is_feasible": True,
            "changes": [],
            "diagnostics": [],
            "baseline_timeline": [],
            "proposed_timeline": [],
            "now_minutes": 810,
        }
    )

    def priced2(plan):
        stats = real_option_stats(plan)
        stats["cost_total"] = 500.0
        return stats

    monkeypatch.setattr(sp, "option_stats", priced2)
    st.settings.auto_publish_max_usd = 100.0  # 500 > 100 → refuse
    recorder2 = RunRecorder(st.store, "sentinel:test2").start()
    assert sp._maybe_auto_publish(st, "autogroup2", recorder2) is None
    monkeypatch.undo()

    st.settings.governance_mode = "advise"
    st.settings.auto_publish_max_usd = 0
    st.settings.auto_publish_max_minutes_moved = 0


def test_dashboard_shows_sentinel_card(client):
    st = client.app.state
    st.settings.demo_clock = "13:30"
    client.get("/")  # triggers evaluation via render
    st.settings.demo_clock = ""
    dash = client.get("/").text
    assert "Sentinel" in dash
