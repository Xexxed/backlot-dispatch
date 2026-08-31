"""Crew-facing routes: tokenized personal schedule pages and acks."""
from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse, RedirectResponse

from app.schedule_view import changes_for_person, compute_calls
from app.tokens import links_valid, sync_token_state

router = APIRouter()

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
