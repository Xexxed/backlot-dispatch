"""Sentinel tick: idempotent agent-raised incident evaluation.

``POST /sentinel/tick`` evaluates every signal and, for any signal carrying
an incident that has not been raised yet today, materializes it through the
EXISTING pipeline (``_incident_pipeline_response`` / its blocking sibling)
with ``source="sentinel"``. Restart-proof by design: no background threads —
evaluate on request; a poller can hit this endpoint every few seconds.

Governance dial (Settings):
  * advise (default) — file incidents, never publish; the human gate decides.
  * auto_low — additionally auto-publish the cheapest feasible option of a
    sentinel-raised group ONLY below both ceilings (max USD exposure and max
    minutes moved); the skipped human gate is trace-logged with its reason.
  * off — evaluation disabled; tick answers immediately with nothing raised.

The sentinel can never mutate a schedule directly — its only write paths are
the sandbox pipeline and the sentinel_state dedupe table.
"""
from __future__ import annotations

import time

from fastapi import APIRouter, Request

from app.serialize import option_stats
from app.sentinel import evaluate
from app.trace import RunRecorder
from app.web.routes_ad import (
    _incident_pipeline_response,
    _now_minutes,
    _price_option,
    _regenerate_qr_artifacts,
    _sandbox_group_for_blocking_incident,
)

router = APIRouter()


def _sentinel_forecasts(st):
    """Resolved per-location forecasts for the imminent-weather signal."""
    from app.weather import read_cache, resolve_forecast

    settings = st.settings
    cache_path = settings.db_path.parent / "weather_cache.json"
    forecasts = {}
    try:
        cached = read_cache(cache_path)
        for loc in st.production.locations.values():
            forecast = resolve_forecast(
                loc.id, settings.seed_dir, cache_path, settings, cache=cached
            )
            if forecast is not None:
                forecasts[loc.id] = forecast
    except Exception:  # noqa: BLE001 - advisory must never break the tick
        return {}
    return forecasts


def _maybe_auto_publish(st, group_id: str, recorder: RunRecorder) -> dict | None:
    """Under auto_low: publish the cheapest feasible option when within both
    ceilings. Returns a summary dict when it published, else None (fail-closed:
    missing cost data or unreachable ceilings never publish)."""
    settings = st.settings
    if settings.governance_mode != "auto_low":
        return None
    if settings.auto_publish_max_usd <= 0 or settings.auto_publish_max_minutes_moved <= 0:
        return None

    plans = st.store.plans_in_group(group_id)
    options = []
    for p in plans:
        cost = _price_option(p, st)
        stats = option_stats(p, cost)
        options.append(stats)
    feasible = [o for o in options if o["is_feasible"]]
    if not feasible:
        return None

    cheapest = min(
        feasible,
        key=lambda o: (
            o.get("cost_total") if isinstance(o.get("cost_total"), (int, float)) else float("inf"),
            o.get("wrap_delta", 0) or 0,
            o.get("moves", 0) or 0,
        ),
    )
    if not isinstance(cheapest.get("cost_total"), (int, float)):
        return None  # no cost ledger here: ceilings cannot be verified
    if cheapest["cost_total"] > settings.auto_publish_max_usd:
        return None
    if (cheapest.get("moves", 0) or 0) > settings.auto_publish_max_minutes_moved:
        return None

    plan = next(p for p in plans if p["strategy"] == cheapest["strategy"])
    with recorder.step("approve", agent="sentinel", kind="human") as stp:
        stp.status = "skipped"
        stp.verdict = "human gate skipped — within ceilings"
        stp.summary = (
            f"auto-published {plan['id']} at ${cheapest['cost_total']} "
            f"({cheapest.get('moves', 0)} moves) under GOVERNANCE_MODE=auto_low"
        )
    if not plan.get("narration"):
        from app.agents.narrator import narrate_plan

        summary_text, source = narrate_plan(plan, st.settings, st.store)
        plan["narration"] = {"text": summary_text, "source": source}
    st.store.save_plan(plan)
    # Mirror publish_plan's ordering: supersede the previous live plan first,
    # then mark this one published (at most one live plan at any instant).
    current = st.store.latest_published_plan()
    if current and current["id"] != plan["id"]:
        st.store.set_plan_status(current["id"], "superseded")
    st.store.set_plan_status(plan["id"], "published")
    return {"plan_id": plan["id"], "cost": cheapest["cost_total"]}


@router.post("/sentinel/tick")
def sentinel_tick(request: Request):
    """Evaluate signals; raise incidents through the standard pipeline.
    Idempotent per (signal, day): unchanged state raises nothing."""
    t0 = time.perf_counter()
    st = request.app.state
    settings = st.settings
    if settings.governance_mode == "off":
        return {"raised": [], "groups": [], "mode": "off"}

    published = st.store.latest_published_plan()
    now_minutes = _now_minutes(None, settings)
    acked_ids: set[str] = set()
    if published:
        acked_ids = {
            a["subject_id"] for a in st.store.acks_for_plan(published["id"])
        }
    forecasts = _sentinel_forecasts(st)
    replies = st.store.unhandled_responses(limit=20)

    signals = evaluate(
        st.production,
        st.rbc,
        published,
        now_minutes,
        settings,
        acked_ids=acked_ids,
        forecasts=forecasts,
        replies=replies,
    )

    day_key = f"{st.production.shoot_date}:{now_minutes // 60:02d}"
    raised: list[dict] = []
    groups: list[str] = []
    for signal in signals:
        if signal.incident is None:
            raised.append(
                {"id": signal.id, "severity": signal.severity, "title": signal.title,
                 "detail": signal.detail, "metric": signal.metric, "incident": False}
            )
            continue
        if st.store.signal_already_raised(signal.id, day_key):
            continue  # idempotent: already filed today
        st.store.record_signal_raised(signal.id, day_key)
        recorder = RunRecorder(st.store, f"sentinel:{signal.id}").start()
        completed_ids = (
            list(published.get("completed_scene_ids") or []) if published else []
        )
        response = _incident_pipeline_response(
            st, signal.incident, completed_ids, now_minutes, t0, recorder
        )
        target = response.headers.get("location", "/")
        if "/sandbox/" in target:
            group_id = target.rsplit("/", 1)[-1]
            groups.append(group_id)
            auto = _maybe_auto_publish(st, group_id, recorder)
            if auto is not None:
                _regenerate_qr_artifacts(request)
            raised.append(
                {"id": signal.id, "severity": signal.severity, "title": signal.title,
                 "detail": signal.detail, "metric": signal.metric, "incident": True,
                 "target": target, "auto_published": auto}
            )
        else:
            recorder.finish("awaiting_human", {"target": target})
            raised.append(
                {"id": signal.id, "severity": signal.severity, "title": signal.title,
                 "detail": signal.detail, "metric": signal.metric, "incident": True,
                 "target": target}
            )
    return {
        "raised": raised,
        "groups": groups,
        "mode": settings.governance_mode,
        "evaluated_ms": round(time.perf_counter() - t0, 3),
    }
