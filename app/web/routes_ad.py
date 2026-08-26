"""AD-side routes: dashboard, incident intake, plan review/publish, reports."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated

import segno
from fastapi import APIRouter, Form, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from app.agents.intake import FallbackRequired, parse_incident
from app.agents.narrator import narrate
from app.engine import baseline_context, replan
from app.models import Incident, hhmm_to_minutes
from app.schedule_view import changes_for_person, compute_calls
from app.serialize import proposal_to_dict, timeline_rows
from app.store import utc_now_iso
from app.timeline import build_timeline
from app.tokens import subject_token
from app.web import STATIC_DIR

router = APIRouter()


def _now_minutes(override: str | None) -> int:
    """Demo clock: explicit HH:MM override wins; otherwise wall clock."""
    if override:
        return hhmm_to_minutes(override)
    now = datetime.now()
    return now.hour * 60 + now.minute


def _token_for(state, kind: str, subject_id: str) -> str:
    return subject_token(state.settings.app_secret, kind, subject_id)


@router.get("/")
def dashboard(request: Request, msg: str = ""):
    st = request.app.state
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

    return st.templates.TemplateResponse(
        request,
        "ad_dashboard.html",
        {
            "production": production,
            "baseline_rows": baseline_rows,
            "published": published,
            "plans": store.list_plans(),
            "people": people,
            "departments": production.departments,
            "ack_count": len(acked_ids),
            "total_people": len(people),
            "acked_ids": acked_ids,
            "msg": msg,
        },
    )


@router.get("/incident")
def incident_form(request: Request):
    st = request.app.state
    return st.templates.TemplateResponse(
        request,
        "incident_form.html",
        {"locations": st.production.locations, "free_text": "", "fallback_reason": None},
    )


@router.post("/incident")
async def create_incident(
    request: Request,
    free_text: Annotated[str, Form()] = "",
    force_manual: Annotated[str, Form()] = "",
    manual_type: Annotated[str, Form()] = "OTHER",
    manual_location: Annotated[str, Form()] = "",
    manual_blocked_until: Annotated[str, Form()] = "",
    manual_severity: Annotated[str, Form()] = "medium",
    completed: Annotated[list[str] | None, Form()] = None,
    now_override: Annotated[str, Form()] = "",
):
    st = request.app.state
    settings, production, store = st.settings, st.production, st.store

    if force_manual == "1":
        incident = Incident(
            type=manual_type or "OTHER",
            location_id=manual_location or None,
            blocked_until=manual_blocked_until or None,
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

    plan_id = uuid.uuid4().hex[:12]
    now_minutes = _now_minutes(now_override or None)
    proposal = replan(
        production,
        st.rbc,
        completed_scene_ids=[c for c in (completed or []) if c],
        incident=incident,
        now_minutes=now_minutes,
        plan_id=plan_id,
        created_at=utc_now_iso(),
    )
    summary_text, narration_source = narrate(
        proposal.changes, proposal.diagnostics, settings, store
    )
    payload = proposal_to_dict(proposal)
    payload["narration"] = {"text": summary_text, "source": narration_source}
    payload["now_minutes"] = now_minutes
    store.save_plan(payload)
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
    return st.templates.TemplateResponse(
        request,
        "plan_diff.html",
        {
            "plan": plan,
            "baseline_rows": baseline_rows,
            "proposed_rows": proposed_rows,
            "moved_ids": moved_ids,
            "msg": msg,
        },
    )


@router.post("/plans/{plan_id}/publish")
async def publish_plan(request: Request, plan_id: str, acknowledge: str = ""):
    st = request.app.state
    plan = st.store.get_plan(plan_id)
    if plan is None:
        return PlainTextResponse("Unknown plan.", status_code=404)

    status = "published_override" if acknowledge == "1" else "published"
    st.store.set_plan_status(plan_id, status)

    # Regenerate per-person links + QR codes into /static/qr/.
    # Prefer the configured canonical origin; the request-derived base is only
    # a fallback and is safe here because TrustedHostMiddleware already
    # rejected any request whose Host header is not on the allowlist.
    base = st.settings.external_base_url or str(request.base_url).rstrip("/")
    for token in st.token_index:
        url = f"{base}/c/{token}"
        segno.make(url, error="m").save(
            str(STATIC_DIR / "qr" / f"{token}.svg"), kind="svg", scale=4, border=2
        )
    note = "" if status == "published" else "+with+acknowledged+violations"
    return RedirectResponse(f"/?msg=Plan+{plan_id}+published{note}", status_code=303)


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
