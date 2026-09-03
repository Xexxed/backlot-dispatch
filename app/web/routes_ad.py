"""AD-side routes: dashboard, incident intake, plan review/publish, reports."""
from __future__ import annotations

import time
import uuid
from datetime import datetime, timezone
from typing import Annotated

import segno
from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import PlainTextResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from app.agents.editor import FallbackRequired as EditFallback, manual_edit_intent, parse_edit_intents
from app.agents.intake import FallbackRequired, parse_incident, parse_incident_voice
from app.agents.narrator import narrate, narrate_plan
from app.edit_ops import EditError, apply_edits
from app.engine import STRATEGIES, baseline_context, baseline_day_items, replan
from app.models import EDIT_ACTIONS, Incident, hhmm_to_minutes
from app.schedule_view import changes_for_person, compute_calls
from app.serialize import option_stats, proposal_to_dict, timeline_rows
from app.store import utc_now_iso
from app.timeline import build_timeline
from app.tokens import (
    links_expire_at,
    links_valid,
    subject_token,
    sync_token_state,
)
from app.weather import (
    build_reports,
    fetch_live,
    read_cache,
    resolve_forecast,
    save_live_forecasts,
)
from app.web import STATIC_DIR

router = APIRouter()


def _now_minutes(override: str | None, settings=None) -> int:
    """Demo clock: explicit HH:MM override wins; then the configured
    NOW_OVERRIDE (timezone-stable demos); otherwise wall clock."""
    if override:
        return hhmm_to_minutes(override)
    configured = getattr(settings, "demo_clock", "") if settings is not None else ""
    if configured:
        return hhmm_to_minutes(configured)
    now = datetime.now()
    return now.hour * 60 + now.minute


def _parse_hhmm_or_none(value: str) -> int | None:
    """Strict HH:MM parse for form input; None when absent or malformed."""
    if not value:
        return None
    try:
        return hhmm_to_minutes(value)
    except ValueError:
        return None


def _token_for(state, kind: str, subject_id: str) -> str:
    return subject_token(state.settings.app_secret, kind, subject_id, state.token_epoch)


def _regenerate_qr_artifacts(request: Request) -> None:
    """Rewrite every crew QR SVG from the current token index.

    Prefer the configured canonical origin; the request-derived base is only
    a fallback and is safe here because TrustedHostMiddleware already
    rejected any request whose Host header is not on the allowlist.
    """
    st = request.app.state
    base = st.settings.external_base_url or str(request.base_url).rstrip("/")
    for token in st.token_index:
        url = f"{base}/c/{token}"
        segno.make(url, error="m").save(
            str(STATIC_DIR / "qr" / f"{token}.svg"), kind="svg", scale=4, border=2
        )


def _recovery_stat(plan: dict | None, settings) -> dict | None:
    if not plan or not isinstance(plan, dict):
        return None
    sec = plan.get("recovery_seconds")
    if not isinstance(sec, (int, float)) or sec <= 0:
        return None

    baseline_min = getattr(settings, "manual_recovery_baseline_minutes", 90)
    baseline_sec = baseline_min * 60
    speedup = baseline_sec / sec
    return {
        "seconds": sec,
        "display": f"{sec:.1f}s",
        "baseline_minutes": baseline_min,
        "baseline_display": f"{baseline_min}m",
        "speedup_display": f"{speedup:.0f}× faster" if speedup >= 10 else f"{speedup:.1f}× faster",
    }


@router.get("/")
def dashboard(request: Request, msg: str = ""):
    st = request.app.state
    sync_token_state(st)  # reflect a rotation performed by another instance
    production, rbc, store = st.production, st.rbc, st.store
    published = store.latest_published_plan()

    base_ctx = baseline_context(production, rbc)
    baseline_rows = timeline_rows(
        build_timeline(list(production.scene_order), base_ctx), production
    )

    calls = compute_calls(production, rbc, published)
    acked_ids: set[str] = set()
    if published:
        acked_ids = {a["subject_id"] for a in store.acks_for_plan(published["id"])}

    subjects = [("crew", m) for m in production.crew] + [
        ("cast", c) for c in production.cast.values()
    ]
    people = []
    for kind, person in subjects:
        card = calls[(kind, person.id)]
        people.append(
            {
                "kind": kind,
                "id": person.id,
                "name": person.name,
                "department": getattr(person, "department", "Cast"),
                "token": _token_for(st, kind, person.id),
                **card,
            }
        )
    people.sort(key=lambda p: (p["department"], p["name"]))

    plans = store.list_plans()
    latest_plan = published or (store.get_plan(plans[0]["id"]) if plans else None)
    recovery_stat = _recovery_stat(latest_plan, st.settings)
    prior_plan = store.previous_published_plan()

    expires = links_expire_at(st.token_issued_at, st.settings.token_ttl_hours)
    links_expiry_display = (
        expires.strftime("%H:%M UTC") if expires is not None else None
    )
    links_expired = not links_valid(st.token_issued_at, st.settings.token_ttl_hours)

    weather_ctx = _weather_context(st, published, base_ctx)

    return st.templates.TemplateResponse(
        request,
        "ad_dashboard.html",
        {
            "production": production,
            "baseline_rows": baseline_rows,
            "published": published,
            "prior_plan": prior_plan,
            "plans": plans,
            "people": people,
            "departments": production.departments,
            "ack_count": len(acked_ids),
            "total_people": len(people),
            "acked_ids": acked_ids,
            "recovery_stat": recovery_stat,
            "links_expiry_display": links_expiry_display,
            "links_expired": links_expired,
            "weather": weather_ctx,
            "msg": msg,
        },
    )


def _weather_context(st, published: dict | None, base_ctx) -> dict | None:
    """Weather-watch card data for the dashboard.

    Fully defensive: any failure (missing fixtures, bad cache, unexpected
    forecast shape) omits the card silently instead of breaking the page.
    Remaining EXT scenes are read from the live published plan when one
    exists (completed scenes already excluded), else the baseline day.
    """
    try:
        settings = st.settings
        production = st.production
        now_minutes = _now_minutes(None, settings)
        timeline = (
            published.get("proposed_timeline")
            if published
            else build_timeline(baseline_day_items(production, st.rbc), base_ctx)
        )
        cache_path = settings.db_path.parent / "weather_cache.json"
        cached = read_cache(cache_path)  # one read per request, not per location
        forecasts = {}
        for loc in production.locations.values():
            forecast = resolve_forecast(
                loc.id, settings.seed_dir, cache_path, settings, cache=cached
            )
            if forecast is not None:
                forecasts[loc.id] = forecast
        if not forecasts:
            return None
        reports = build_reports(production, timeline, forecasts, now_minutes, settings)
        return {
            "reports": reports,
            "now": now_minutes,
            "live_configured": bool(settings.google_maps_api_key),
            "precip_pct": settings.weather_precip_pct,
            "wind_kmh": settings.weather_wind_kmh,
        }
    except Exception:  # noqa: BLE001 - advisory must never break the dashboard
        import traceback

        traceback.print_exc()
        return None


@router.get("/incident")
def incident_form(request: Request):
    st = request.app.state
    return st.templates.TemplateResponse(
        request,
        "incident_form.html",
        {
            "locations": st.production.locations,
            "free_text": "",
            "fallback_reason": None,
            "error": None,
        },
    )


@router.post("/incident")
async def create_incident(
    request: Request,
    free_text: Annotated[str, Form()] = "",
    force_manual: Annotated[str, Form()] = "",
    manual_type: Annotated[str, Form()] = "OTHER",
    manual_location: Annotated[str, Form()] = "",
    manual_blocked_until: Annotated[str, Form()] = "",
    manual_blocked_from: Annotated[str, Form()] = "",
    manual_severity: Annotated[str, Form()] = "medium",
    completed: Annotated[list[str] | None, Form()] = None,
    now_override: Annotated[str, Form()] = "",
):
    t0 = time.perf_counter()
    st = request.app.state
    settings, production, store = st.settings, st.production, st.store

    if force_manual == "1":
        blocked_until_min = _parse_hhmm_or_none(manual_blocked_until)
        blocked_from_min = _parse_hhmm_or_none(manual_blocked_from)
        invalid_window = (
            (manual_blocked_until and blocked_until_min is None)
            or (manual_blocked_from and blocked_from_min is None)
            or (
                blocked_until_min is not None
                and blocked_from_min is not None
                and blocked_from_min >= blocked_until_min
            )
        )
        if invalid_window:
            return st.templates.TemplateResponse(
                request,
                "incident_form.html",
                {
                    "locations": production.locations,
                    "free_text": free_text,
                    "fallback_reason": None,
                    "error": (
                        "Invalid blocked window — use HH:MM and make "
                        "'blocked from' earlier than 'blocked until'."
                    ),
                },
            )
        incident = Incident(
            type=manual_type or "OTHER",
            location_id=manual_location or None,
            blocked_until=manual_blocked_until or None,
            blocked_from=manual_blocked_from or None,
            severity=manual_severity or "medium",
            free_text=free_text or "(manual form)",
            confidence=1.0,
            source="manual_form",
        )
    else:
        try:
            incident = parse_incident(free_text, settings, production.locations, store)
        except FallbackRequired as fr:
            return st.templates.TemplateResponse(
                request,
                "incident_form.html",
                {
                    "locations": production.locations,
                    "free_text": free_text,
                    "fallback_reason": fr.reason,
                },
            )

    now_minutes = _now_minutes(now_override or None, settings)
    return _incident_pipeline_response(
        st, incident, [c for c in (completed or []) if c], now_minutes, t0
    )


def _incident_pipeline_response(
    st, incident: Incident, completed_ids: list[str], now_minutes: int, t0: float
) -> RedirectResponse:
    """Post-intake flow shared by the text route and the voice route.

    Non-blocking incidents (cast delay, weather w/o location, etc.) go straight
    to a single review page — the sandbox only meaningfully differs when a
    location is actually blocked. Blocking incidents generate every recovery
    strategy as a sandbox group.
    """
    if not (incident.location_id and incident.blocked_until_minutes() is not None):
        plan_id = uuid.uuid4().hex[:12]
        proposal = replan(
            st.production,
            st.rbc,
            completed_scene_ids=completed_ids,
            incident=incident,
            now_minutes=now_minutes,
            plan_id=plan_id,
            created_at=utc_now_iso(),
        )
        summary_text, narration_source = narrate(
            proposal.changes, proposal.diagnostics, st.settings, st.store
        )
        payload = proposal_to_dict(proposal)
        payload["narration"] = {"text": summary_text, "source": narration_source}
        payload["now_minutes"] = now_minutes
        payload["recovery_seconds"] = max(round(time.perf_counter() - t0, 2), 0.05)
        st.store.save_plan(payload)
        return RedirectResponse(f"/plans/{plan_id}", status_code=303)

    group_id = _sandbox_group_for_blocking_incident(
        st, incident, completed_ids, now_minutes, t0
    )
    return RedirectResponse(f"/sandbox/{group_id}", status_code=303)


# ~5 minutes of 16 kHz 16-bit mono WAV; the client widget caps recording at
# ~2 minutes, so anything larger is an abuse/bug case, not a real note.
MAX_VOICE_NOTE_BYTES = 10 * 1024 * 1024
# Content-Length pre-check: multipart framing adds a small fixed overhead on
# top of the file part, and a body far beyond that is rejected before the
# upload is read into memory at all.
_MAX_VOICE_REQUEST_BYTES = MAX_VOICE_NOTE_BYTES + 64 * 1024


def _looks_like_wav(data: bytes) -> bool:
    return len(data) >= 44 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"


@router.post("/incident/voice")
async def create_incident_voice(
    request: Request,
    audio: Annotated[UploadFile | None, File()] = None,
    completed: Annotated[list[str] | None, Form()] = None,
    now_override: Annotated[str, Form()] = "",
):
    """Voice-note intake: one Gemini call transcribes + extracts the incident,
    then the exact post-intake flow of /incident runs. Validation errors and
    agent fallbacks re-render the form with HTTP 200, same convention as the
    text path. Audio is processed in memory and never persisted.
    """
    t0 = time.perf_counter()
    st = request.app.state
    settings, production, store = st.settings, st.production, st.store

    def _rerender(error: str | None = None, fallback_reason: str | None = None):
        return st.templates.TemplateResponse(
            request,
            "incident_form.html",
            {
                "locations": production.locations,
                "free_text": "",
                "fallback_reason": fallback_reason,
                "error": error,
            },
        )

    content_length = request.headers.get("content-length")
    if (
        content_length
        and content_length.isdigit()
        and int(content_length) > _MAX_VOICE_REQUEST_BYTES
    ):
        return _rerender(
            error="Voice note too long — keep the report under two minutes."
        )

    # Read at most cap+1 bytes: the size check below then always fires for an
    # oversize body without ever buffering more than the cap, even if the
    # client lied about Content-Length (or used chunked encoding).
    data = await audio.read(MAX_VOICE_NOTE_BYTES + 1) if audio is not None else b""
    if not data:
        return _rerender(error="No audio received — record a voice note and try again.")
    if len(data) > MAX_VOICE_NOTE_BYTES:
        return _rerender(
            error="Voice note too long — keep the report under two minutes."
        )
    if not _looks_like_wav(data):
        return _rerender(
            error="Unsupported audio format — voice notes are captured as WAV."
        )

    try:
        # The Gemini call is a blocking multi-second round trip on multi-MB
        # audio; the deterministic replan/narrate pipeline follows it. Both
        # run off the event loop so a voice upload cannot stall the single
        # uvicorn worker for every other request.
        incident = await run_in_threadpool(
            parse_incident_voice, data, settings, production.locations, store
        )
    except FallbackRequired as fr:
        return _rerender(fallback_reason=fr.reason)

    now_minutes = _now_minutes(now_override or None, settings)
    return await run_in_threadpool(
        _incident_pipeline_response,
        st,
        incident,
        [c for c in (completed or []) if c],
        now_minutes,
        t0,
    )


def _sandbox_group_for_blocking_incident(
    st, incident: Incident, completed_ids: list[str], now_minutes: int, t0: float
) -> str:
    """Run every recovery strategy for a blocking incident and persist the
    resulting sandbox group. Shared by /incident and /weather/adopt so both
    paths produce identical option sets. Returns the group id."""
    group_id = uuid.uuid4().hex[:12]
    for strategy_id in STRATEGIES:
        option_id = uuid.uuid4().hex[:12]
        proposal = replan(
            st.production,
            st.rbc,
            completed_scene_ids=completed_ids,
            incident=incident,
            now_minutes=now_minutes,
            plan_id=option_id,
            created_at=utc_now_iso(),
            strategy=strategy_id,
            group_id=group_id,
        )
        payload = proposal_to_dict(proposal)
        payload["now_minutes"] = now_minutes
        st.store.save_plan(payload)
    recovery_seconds = max(round(time.perf_counter() - t0, 2), 0.05)
    for option in st.store.plans_in_group(group_id):
        option["recovery_seconds"] = recovery_seconds
        st.store.save_plan(option)
    return group_id


@router.post("/weather/refresh")
def weather_refresh(request: Request):
    """Manual live-forecast pull: Google Weather API -> runtime cache.

    Fixture-first design: this is the ONLY network path, it runs on demand,
    and failures keep the previous cache/fixtures untouched. Deliberately a
    sync handler: the blocking HTTP fetches then run in FastAPI's threadpool
    instead of stalling the event loop.
    """
    st = request.app.state
    settings = st.settings
    if not settings.google_maps_api_key:
        return RedirectResponse(
            "/?msg=Live+forecast+not+configured+-+set+GOOGLE_MAPS_API_KEY;"
            "+committed+fixtures+stay+active",
            status_code=303,
        )
    cache_path = settings.db_path.parent / "weather_cache.json"
    coordinated = [
        loc
        for loc in st.production.locations.values()
        if loc.lat is not None and loc.lng is not None
    ]
    results = {}
    for loc in coordinated:
        hours = fetch_live(loc, settings.google_maps_api_key)
        if hours:
            results[loc.id] = hours
    msg = "Live+forecast+fetch+failed+-+keeping+current+fixtures/cache"
    if results:
        try:
            save_live_forecasts(cache_path, results, datetime.now(timezone.utc))
        except OSError:
            # e.g. the atomic replace raced a concurrent cache reader on
            # Windows, or the runtime dir is read-only: keep the previous
            # forecast data rather than failing the request.
            pass
        else:
            msg = f"Forecast+refreshed+-+live+from+Google+Weather+({len(results)}+of+{len(coordinated)}+locations)"
    return RedirectResponse(f"/?msg={msg}", status_code=303)


@router.post("/weather/adopt")
async def weather_adopt(
    request: Request,
    location_id: Annotated[str, Form()] = "",
    window_from: Annotated[str, Form(alias="from")] = "",
    window_until: Annotated[str, Form(alias="until")] = "",
    summary: Annotated[str, Form()] = "",
):
    """One-click pivot: materialize an advisory as a blocking WEATHER incident
    and run the standard strategy sandbox for it."""
    t0 = time.perf_counter()
    st = request.app.state
    location = st.production.locations.get(location_id)
    start = _parse_hhmm_or_none(window_from)
    until = _parse_hhmm_or_none(window_until)
    if location is None or start is None or until is None or start >= until:
        return RedirectResponse(
            "/?msg=Weather+advisory+rejected+-+invalid+window;"
            "+refresh+the+forecast+and+retry",
            status_code=303,
        )
    incident = Incident(
        type="WEATHER",
        location_id=location.id,
        blocked_from=window_from,
        blocked_until=window_until,
        severity="high",
        free_text=(
            f"Weather advisory: {location.name} hazardous "
            f"{window_from}–{window_until}"
            + (f" ({summary})" if summary else "")
        ),
        confidence=1.0,
        source="weather_advisor",
    )
    now_minutes = _now_minutes(None, st.settings)
    published = st.store.latest_published_plan()
    completed_ids = list(published.get("completed_scene_ids") or []) if published else []
    group_id = _sandbox_group_for_blocking_incident(
        st, incident, completed_ids, now_minutes, t0
    )
    return RedirectResponse(f"/sandbox/{group_id}", status_code=303)


def _edit_response(
    request: Request,
    st,
    free_text: str = "",
    fallback_reason: str | None = None,
    error: str | None = None,
):
    return st.templates.TemplateResponse(
        request,
        "edit_form.html",
        {
            "locations": st.production.locations,
            "scenes": st.production.scenes,
            "scene_order": st.production.scene_order,
            "free_text": free_text,
            "fallback_reason": fallback_reason,
            "error": error,
        },
    )


@router.get("/edit")
def edit_form(request: Request):
    return _edit_response(request, request.app.state)


@router.post("/edit")
async def create_edit(
    request: Request,
    free_text: Annotated[str, Form()] = "",
    force_manual: Annotated[str, Form()] = "",
    manual_action: Annotated[str, Form()] = "",
    manual_scene: Annotated[str, Form()] = "",
    manual_ref_scene: Annotated[str, Form()] = "",
    manual_location: Annotated[str, Form()] = "",
    manual_title: Annotated[str, Form()] = "",
    manual_pages: Annotated[str, Form()] = "1",
    manual_int_ext: Annotated[str, Form()] = "INT",
    manual_day_night: Annotated[str, Form()] = "DAY",
    completed: Annotated[list[str] | None, Form()] = None,
    now_override: Annotated[str, Form()] = "",
):
    st = request.app.state
    settings, production, store = st.settings, st.production, st.store

    try:
        if force_manual == "1":
            if manual_action not in EDIT_ACTIONS:
                return _edit_response(
                    request, st, free_text=free_text,
                    error="Pick an edit action (move, relocate, or add).",
                )
            edits = [
                manual_edit_intent(
                    action=manual_action,
                    scene_id=manual_scene,
                    ref_scene_id=manual_ref_scene,
                    new_location_id=manual_location,
                    title=manual_title,
                    page_count=manual_pages,
                    location_id=manual_location,
                    int_ext=manual_int_ext,
                    day_night=manual_day_night,
                )
            ]
        else:
            edits = parse_edit_intents(free_text, settings, production, store)

        now_minutes = _now_minutes(now_override or None, settings)
        proposal = apply_edits(
            production,
            st.rbc,
            completed_scene_ids=[c for c in (completed or []) if c],
            edits=edits,
            now_minutes=now_minutes,
            plan_id=uuid.uuid4().hex[:12],
            created_at=utc_now_iso(),
        )
    except EditFallback as fr:
        return _edit_response(request, st, free_text=free_text, fallback_reason=fr.reason)
    except EditError as ee:
        return _edit_response(request, st, free_text=free_text, error=str(ee))

    summary_text, narration_source = narrate(
        proposal.changes, proposal.diagnostics, settings, store
    )
    payload = proposal_to_dict(proposal)
    payload["narration"] = {"text": summary_text, "source": narration_source}
    payload["now_minutes"] = now_minutes
    store.save_plan(payload)
    return RedirectResponse(f"/plans/{proposal.id}", status_code=303)


@router.get("/sandbox/{group_id}")
def sandbox(request: Request, group_id: str, msg: str = ""):
    st = request.app.state
    plans = st.store.plans_in_group(group_id)
    if not plans:
        return PlainTextResponse("Unknown scenario.", status_code=404)
    order_index = {sid: i for i, sid in enumerate(STRATEGIES)}
    options = []
    for p in plans:
        stats = option_stats(p)
        strat = STRATEGIES.get(stats["strategy"])
        stats["strategy_label"] = strat.name if strat else stats["strategy"]
        stats["tagline"] = strat.tagline if strat else ""
        stats["plan"] = p
        options.append(stats)
    options.sort(key=lambda o: order_index.get(o["strategy"], 99))
    published_id = next(
        (p["id"] for p in plans if p["status"].startswith("published")), None
    )
    return st.templates.TemplateResponse(
        request,
        "sandbox.html",
        {
            "group_id": group_id,
            "incident": plans[0].get("incident", {}),
            "recovery_stat": _recovery_stat(plans[0], st.settings),
            "options": options,
            "published_id": published_id,
            "msg": msg,
        },
    )


@router.post("/plans/{plan_id}/select")
async def select_option(request: Request, plan_id: str):
    st = request.app.state
    plan = st.store.get_plan(plan_id)
    if plan is None:
        return PlainTextResponse("Unknown plan.", status_code=404)
    group_id = plan.get("group_id") or ""
    if not group_id:
        return RedirectResponse(f"/plans/{plan_id}", status_code=303)
    if st.store.group_has_published(group_id):
        return RedirectResponse(
            f"/sandbox/{group_id}?msg=Scenario+already+published", status_code=303
        )
    if not plan.get("narration"):
        summary_text, narration_source = narrate_plan(plan, st.settings, st.store)
        plan["narration"] = {"text": summary_text, "source": narration_source}
    plan["status"] = "proposed"
    st.store.save_plan(plan)
    for other in st.store.plans_in_group(group_id):
        if other["id"] != plan_id and other["status"] == "proposed":
            st.store.set_plan_status(other["id"], "alternative")
    return RedirectResponse(f"/plans/{plan_id}", status_code=303)


@router.get("/plans/{plan_id}")
def plan_diff(request: Request, plan_id: str, msg: str = ""):
    st = request.app.state
    plan = st.store.get_plan(plan_id)
    if plan is None:
        return PlainTextResponse("Unknown plan.", status_code=404)
    baseline_rows = timeline_rows(plan["baseline_timeline"], st.production)
    proposed_rows = timeline_rows(plan["proposed_timeline"], st.production)
    moved_ids = {c["scene_id"] for c in plan["changes"] if c.get("scene_id")}
    recovery_stat = _recovery_stat(plan, st.settings)
    return st.templates.TemplateResponse(
        request,
        "plan_diff.html",
        {
            "plan": plan,
            "baseline_rows": baseline_rows,
            "proposed_rows": proposed_rows,
            "moved_ids": moved_ids,
            "recovery_stat": recovery_stat,
            "msg": msg,
        },
    )


@router.post("/plans/{plan_id}/publish")
async def publish_plan(request: Request, plan_id: str, acknowledge: str = ""):
    st = request.app.state
    sync_token_state(st)
    plan = st.store.get_plan(plan_id)
    if plan is None:
        return PlainTextResponse("Unknown plan.", status_code=404)

    status = "published_override" if acknowledge == "1" else "published"
    # Narrate on publish if the option was published directly without selection.
    if not plan.get("narration"):
        summary_text, narration_source = narrate_plan(plan, st.settings, st.store)
        plan["narration"] = {"text": summary_text, "source": narration_source}
        st.store.save_plan(plan)

    group_id = plan.get("group_id") or ""
    if group_id:
        for other in st.store.plans_in_group(group_id):
            if other["id"] != plan_id and other["status"] == "proposed":
                st.store.set_plan_status(other["id"], "alternative")

    # Versioning: the previously live plan is kept (superseded) as the
    # one-click rollback target — agents propose, humans can undo.
    current = st.store.latest_published_plan()
    if current and current["id"] != plan_id:
        st.store.set_plan_status(current["id"], "superseded")

    st.store.set_plan_status(plan_id, status)

    # Regenerate per-person links + QR codes into /static/qr/.
    _regenerate_qr_artifacts(request)
    note = "" if status == "published" else "+with+acknowledged+violations"
    return RedirectResponse(f"/?msg=Plan+{plan_id}+published{note}", status_code=303)


@router.post("/plans/{plan_id}/revert")
async def revert_plan(request: Request, plan_id: str):
    """One-click rollback: restore a previously published (superseded) plan as
    the live one. Crew links are token-based and the portal renders whatever
    plan is currently published, so this swaps instantly — then QR artifacts
    are regenerated for consistency."""
    st = request.app.state
    plan = st.store.get_plan(plan_id)
    if plan is None:
        return PlainTextResponse("Unknown plan.", status_code=404)
    if plan["status"].startswith("published"):
        return RedirectResponse(
            f"/?msg=Plan+{plan_id}+is+already+the+live+plan", status_code=303
        )

    current = st.store.latest_published_plan()
    if current and current["id"] != plan_id:
        st.store.set_plan_status(current["id"], "superseded")
    st.store.set_plan_status(plan_id, "published")

    sync_token_state(st)
    _regenerate_qr_artifacts(request)
    return RedirectResponse(
        f"/?msg=Reverted+to+plan+{plan_id}+-+crew+links+live+again", status_code=303
    )


@router.post("/links/rotate")
async def rotate_links(request: Request):
    """One-shot crew-link rotation: bump the token epoch.

    Every previously distributed personal link and QR code stops resolving
    (404) immediately; fresh links + QR SVGs are generated in their place.
    Use when a link leaked or the TTL lapsed mid-day.
    """
    st = request.app.state
    st.store.bump_token_epoch()
    sync_token_state(st)  # adopt the new epoch: rebuild index + issue time

    # Drop stale QR artifacts so a scanned old code can't linger on disk,
    # then regenerate from the new epoch. If the process dies between the
    # bump and regeneration, create_app's QR reconciliation repairs it on
    # the next boot.
    for old in (STATIC_DIR / "qr").glob("*.svg"):
        old.unlink()
    _regenerate_qr_artifacts(request)
    return RedirectResponse(
        "/?msg=Crew+links+rotated+-+old+links+and+QRs+are+dead", status_code=303
    )


@router.get("/changelog")
def changelog(request: Request):
    st = request.app.state
    return st.templates.TemplateResponse(
        request,
        "changelog.html",
        {"published": st.store.latest_published_plan(), "plans": st.store.list_plans()},
    )


@router.get("/dept/{dept_name:path}")
def department_view(request: Request, dept_name: str):
    st = request.app.state
    sync_token_state(st)
    production = st.production
    crew = [m for m in production.crew if m.department.lower() == dept_name.lower()]
    if not crew:
        return PlainTextResponse("Unknown department.", status_code=404)
    published = st.store.latest_published_plan()
    calls = compute_calls(production, st.rbc, published)
    members = []
    for m in crew:
        card = calls[("crew", m.id)]
        my_changes = (
            changes_for_person(published["changes"], production, "crew", m.id)
            if published
            else []
        )
        members.append(
            {"member": m, "token": _token_for(st, "crew", m.id), "changes": my_changes, **card}
        )
    members.sort(key=lambda x: x["member"].name)
    return st.templates.TemplateResponse(
        request,
        "department.html",
        {"dept": dept_name, "members": members, "published": published},
    )
