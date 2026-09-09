"""Crew-facing routes: tokenized personal schedule pages, acks, and replies."""
from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import PlainTextResponse, RedirectResponse
from starlette.concurrency import run_in_threadpool

from app.agents.responder import (
    FallbackRequired as ResponderFallback,
    CrewResponse,
    manual_response,
    parse_reply,
    parse_reply_voice,
)
from app.schedule_view import changes_for_person, compute_calls
from app.sentinel import is_critical_role
from app.tokens import links_valid, sync_token_state

router = APIRouter()

# ~5 minutes of 16 kHz 16-bit mono WAV — same abuse bound as AD voice intake.
MAX_REPLY_AUDIO_BYTES = 10 * 1024 * 1024

_EXPIRED = PlainTextResponse(
    "This link expired — ask the AD for a fresh one.", status_code=410
)


def _resolve(request: Request, token: str):
    """(kind, subject_id) for a live token, or a PlainTextResponse error."""
    st = request.app.state
    sync_token_state(st)  # pick up rotations performed by another instance
    subject = st.token_index.get(token)
    if subject is None:
        return PlainTextResponse("Unknown link — ask the AD for your personal link.", 404)
    if not links_valid(st.token_issued_at, st.settings.token_ttl_hours):
        return _EXPIRED
    return subject


def _person(production, kind: str, subject_id: str):
    if kind == "crew":
        return next((m for m in production.crew if m.id == subject_id), None)
    return production.cast.get(subject_id)


def _save_reply(st, token: str, subject, reply: CrewResponse) -> None:
    """Persist the structured reply against the live published plan."""
    published = st.store.latest_published_plan()
    person = _person(st.production, subject[0], subject[1])
    critical = is_critical_role(getattr(person, "role", None))
    st.store.save_response(
        response_id=uuid.uuid4().hex[:12],
        plan_id=published["id"] if published else "",
        token=token,
        subject_id=subject[1],
        kind=reply.kind,
        eta_minutes=reply.eta_minutes,
        leave_by=reply.leave_by,
        free_text=(reply.transcript or "")[:280],
        critical=critical,
    )


@router.get("/c/{token}")
def crew_card(request: Request, token: str, msg: str = ""):
    st = request.app.state
    subject = _resolve(request, token)
    if not isinstance(subject, tuple):
        return subject
    kind, subject_id = subject
    person = _person(st.production, kind, subject_id)
    if person is None:
        return PlainTextResponse("Unknown link.", 404)

    published = st.store.latest_published_plan()
    calls = compute_calls(st.production, st.rbc, published)
    card = calls[(kind, subject_id)]
    my_changes = (
        changes_for_person(published["changes"], st.production, kind, subject_id)
        if published
        else []
    )
    acked = published is not None and st.store.has_acked(published["id"], subject_id)
    return st.templates.TemplateResponse(
        request,
        "crew_card.html",
        {
            "kind": kind,
            "person": person,
            "token": token,
            "department": getattr(person, "department", "Cast"),
            "role": getattr(person, "role", getattr(person, "character", "")),
            "card": card,
            "published": published,
            "my_changes": my_changes,
            "acked": acked,
            "msg": msg,
            "production": st.production,
            "ad_nav": False,  # crew have no AD credentials — hide console links
        },
    )


@router.post("/c/{token}/ack")
def acknowledge(request: Request, token: str, message: str = ""):
    st = request.app.state
    subject = _resolve(request, token)
    if not isinstance(subject, tuple):
        return subject
    kind, subject_id = subject
    published = st.store.latest_published_plan()
    if published is None:
        return RedirectResponse(f"/c/{token}?msg=Nothing+to+acknowledge+yet", status_code=303)
    person = _person(st.production, kind, subject_id)
    st.store.record_ack(
        published["id"],
        token,
        subject_id=subject_id,
        display_name=getattr(person, "name", subject_id),
        message=message[:280],
    )
    return RedirectResponse(f"/c/{token}?msg=Acknowledged+-+thank+you", status_code=303)


# ------------------------------------------------------------- crew replies
def _reply_route(st, token: str, subject, reply: CrewResponse) -> RedirectResponse:
    """Shared persistence for text and voice replies."""
    _save_reply(st, token, subject, reply)
    name = _person(st.production, subject[0], subject[1])
    display = getattr(name, "name", subject[1])
    return RedirectResponse(
        f"/c/{token}?msg=Reply+received+-+the+AD+has+been+notified+({display})",
        status_code=303,
    )


@router.post("/c/{token}/reply")
async def reply_text(
    request: Request,
    token: str,
    free_text: Annotated[str, Form()] = "",
    manual_kind: Annotated[str, Form()] = "",
    manual_eta_minutes: Annotated[str, Form()] = "",
    manual_leave_by: Annotated[str, Form()] = "",
):
    """Inbound crew reply (text). Gemini path classifies free text; the
    manual quick-reply fields bypass the agent entirely (no credentials
    needed) — the two paths produce identical structured replies."""
    st = request.app.state
    subject = _resolve(request, token)
    if not isinstance(subject, tuple):
        return subject
    if st.store.latest_published_plan() is None:
        return RedirectResponse(f"/c/{token}?msg=Nothing+to+reply+to+yet", status_code=303)

    if manual_kind:  # explicit quick-reply path — no agent round trip
        reply = manual_response(
            manual_kind, free_text=free_text,
            eta_minutes=manual_eta_minutes, leave_by=manual_leave_by,
        )
    else:
        try:
            reply = await run_in_threadpool(
                parse_reply, free_text, st.settings, st.store
            )
        except ResponderFallback as fr:
            return RedirectResponse(
                f"/c/{token}?msg={fr.reason.replace(' ', '+')[:200]}", status_code=303
            )
    return _reply_route(st, token, subject, reply)


@router.post("/c/{token}/reply/voice")
async def reply_voice(request: Request, token: str, audio: UploadFile | None = File(None)):
    """Inbound crew voice reply: WAV in memory → one Gemini call → structured
    reply. The audio itself is NEVER persisted (same rule as incident voice)."""
    st = request.app.state
    subject = _resolve(request, token)
    if not isinstance(subject, tuple):
        return subject
    if st.store.latest_published_plan() is None:
        return RedirectResponse(f"/c/{token}?msg=Nothing+to+reply+to+yet", status_code=303)

    data = await audio.read(MAX_REPLY_AUDIO_BYTES + 1) if audio is not None else b""
    if not data or len(data) > MAX_REPLY_AUDIO_BYTES:
        return RedirectResponse(
            f"/c/{token}?msg=Recording+problem+-+try+again+or+use+the+buttons",
            status_code=303,
        )
    if not (len(data) >= 44 and data[:4] == b"RIFF" and data[8:12] == b"WAVE"):
        return RedirectResponse(
            f"/c/{token}?msg=Unsupported+audio+format+-+replies+are+captured+as+WAV",
            status_code=303,
        )
    try:
        reply = await run_in_threadpool(parse_reply_voice, data, st.settings, st.store)
    except ResponderFallback as fr:
        return RedirectResponse(
            f"/c/{token}?msg={fr.reason.replace(' ', '+')[:200]}", status_code=303
        )
    return _reply_route(st, token, subject, reply)
