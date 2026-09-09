"""Crew Response Agent + Exceptions queue.

Invariants under test (plan §4):
  * manual fallback output is schema-identical to the Gemini path (parity);
  * raw audio is NEVER persisted (memory only; logs carry lengths);
  * personal narrative never renders on any AD-facing page beyond the
    structured fields;
  * critical-role cannot_make / late>30 offers recovery options; non-critical
    does not;
  * token expiry and rotation reject replies exactly like acks.
"""
from __future__ import annotations

import base64

from app.agents.responder import manual_response
from app.sentinel import CRITICAL_ROLES, is_critical_role


# ------------------------------------------------------------- manual parity
def test_manual_response_schema_parity():
    """The manual builder carries exactly the Gemini-path fields."""
    manual = manual_response("running_late", free_text="stuck in traffic", eta_minutes="30")
    assert manual.kind == "running_late"
    assert manual.eta_minutes == 30
    assert manual.leave_by is None
    assert manual.transcript == "stuck in traffic"
    assert manual.source == "manual"
    assert manual.confidence == 1.0


def test_manual_response_normalizes_and_coerces():
    assert manual_response("weird-kind").kind == "question"  # unknown → question
    assert manual_response("cannot_make", leave_by="9:05").leave_by == "09:05"
    assert manual_response("running_late", eta_minutes="-5").eta_minutes == 0
    assert manual_response("running_late", eta_minutes="abc").eta_minutes is None


def test_critical_roles_match_seed_vocabulary():
    assert is_critical_role("DP")
    assert is_critical_role("1st AD")
    assert is_critical_role("sound mixer")
    assert is_critical_role("Key Grip")
    assert is_critical_role("Script Supervisor")
    assert not is_critical_role("Set PA")
    assert not is_critical_role(None)
    assert not is_critical_role("")


def _first_token(client, department: str) -> str:
    """Personal token of the first crew member in a department."""
    st = client.app.state
    for m in st.production.crew:
        if m.department.lower() == department.lower():
            from app.tokens import subject_token

            return subject_token(st.settings.app_secret, "crew", m.id, st.token_epoch)
    raise AssertionError(f"no crew in {department}")


def _publish_a_plan(client) -> None:
    """File a non-blocking incident (single review page) and publish it, so
    crew replies have a live plan to attach to."""
    resp = client.post(
        "/incident",
        data={
            "force_manual": "1",
            "manual_type": "CAST_DELAY",
            "free_text": "Lead stuck in traffic",
            "now_override": "11:40",
        },
        follow_redirects=False,
    )
    assert resp.status_code == 303
    plan_id = resp.headers["location"].rsplit("/", 1)[-1]
    resp = client.post(f"/plans/{plan_id}/publish", follow_redirects=False)
    assert resp.status_code == 303
    assert client.app.state.store.latest_published_plan() is not None


def test_reply_roundtrip_manual_and_queue_visible(client):
    """A crew member replies via the quick-reply form; the dashboard queue
    shows the structured fields only — never a narrative."""
    token = _first_token(client, "Camera")
    member = next(
        m for m in client.app.state.production.crew
        if m.department.lower() == "camera"
    )
    # ensure a published plan exists
    _publish_a_plan(client)
    resp = client.post(
        f"/c/{token}/reply",
        data={"manual_kind": "running_late", "manual_eta_minutes": "20"},
        follow_redirects=False,
    )
    assert resp.status_code == 303

    dash = client.get("/").text
    assert member.name in dash
    assert "running_late" in dash
    # privacy: the (empty) narrative of a time-fact reply is not rendered
    assert "stuck in traffic" not in dash


def test_late_critical_reply_offers_recovery_non_critical_does_not(client):
    """cannot_make from a critical role → 'Generate recovery options';
    the same reply from a non-critical role → mark-handled only."""
    critical_token = None
    plain_token = None
    st = client.app.state
    from app.tokens import subject_token

    for m in st.production.crew:
        tok = subject_token(st.settings.app_secret, "crew", m.id, st.token_epoch)
        if is_critical_role(m.role) and critical_token is None:
            critical_token = tok
        if not is_critical_role(m.role) and plain_token is None:
            plain_token = tok
    assert critical_token and plain_token

    _publish_a_plan(client)
    for tok in (critical_token, plain_token):
        resp = client.post(
            f"/c/{tok}/reply",
            data={"manual_kind": "cannot_make", "manual_leave_by": "14:30"},
            follow_redirects=False,
        )
        assert resp.status_code == 303

    responses = client.app.state.store.unhandled_responses()
    by_critical = {r["critical"]: r for r in responses}
    assert True in by_critical and False in by_critical

    dash = client.get("/").text
    assert "Generate recovery options" in dash  # critical row has the button
    assert dash.count("Generate recovery options") == 1  # only the critical one


def test_generate_recovery_options_files_incident_not_mutation(client):
    """The queue button produces a reviewable plan via the standard pipeline;
    it never publishes or mutates any existing plan."""
    st = client.app.state
    from app.tokens import subject_token

    member = next(m for m in st.production.crew if is_critical_role(m.role))
    token = subject_token(st.settings.app_secret, "crew", member.id, st.token_epoch)
    _publish_a_plan(client)
    client.post(
        f"/c/{token}/reply",
        data={"manual_kind": "cannot_make", "manual_leave_by": "14:30"},
        follow_redirects=False,
    )
    response = st.store.unhandled_responses()[0]
    plans_before = {p["id"]: p for p in st.store.list_plans(limit=100)}

    resp = client.post(f"/responses/{response['id']}/recover", follow_redirects=False)
    assert resp.status_code == 303
    target = resp.headers["location"]
    assert target.startswith("/plans/")  # CAST_DELAY → single review page

    assert st.store.get_response(response["id"])["handled"] == 1
    # the new plan is proposed (not published); old plans untouched
    plans_after = {p["id"]: p for p in st.store.list_plans(limit=100)}
    new_id = target.rsplit("/", 1)[-1]
    new_plan = st.store.get_plan(new_id)
    assert new_plan["status"] == "proposed"
    assert new_plan["incident"]["type"] == "CAST_DELAY"
    for pid, old in plans_before.items():
        assert plans_after[pid]["status"] == old["status"]


def test_mark_handled(client):
    st = client.app.state
    from app.tokens import subject_token

    member = next(m for m in st.production.crew if is_critical_role(m.role))
    token = subject_token(st.settings.app_secret, "crew", member.id, st.token_epoch)
    _publish_a_plan(client)
    client.post(
        f"/c/{token}/reply",
        data={"manual_kind": "question", "free_text": "Is lunch moving?"},
        follow_redirects=False,
    )
    response = st.store.unhandled_responses()[0]
    assert response["kind"] == "question"
    resp = client.post(f"/responses/{response['id']}/handle", follow_redirects=False)
    assert resp.status_code == 303
    assert st.store.unhandled_responses() == []


def test_reply_needs_published_plan_and_valid_token(client):
    st = client.app.state
    from app.tokens import subject_token

    member = next(m for m in st.production.crew)
    token = subject_token(st.settings.app_secret, "crew", member.id, st.token_epoch)

    # no published plan yet → reply refused
    resp = client.post(
        f"/c/{token}/reply",
        data={"manual_kind": "ack"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "Nothing+to+reply+to" in resp.headers["location"]

    # publish, then rotate links: the old token must stop working
    _publish_a_plan(client)
    client.post("/links/rotate", follow_redirects=False)
    resp = client.post(
        f"/c/{token}/reply",
        data={"manual_kind": "ack"},
        follow_redirects=False,
    )
    assert resp.status_code == 404


def test_voice_reply_rejects_non_wav_without_persisting(client):
    st = client.app.state
    from app.tokens import subject_token

    member = next(m for m in st.production.crew if is_critical_role(m.role))
    token = subject_token(st.settings.app_secret, "crew", member.id, st.token_epoch)
    _publish_a_plan(client)
    junk = base64.b64encode(b"this is definitely not a RIFF/WAVE payload......")
    resp = client.post(
        f"/c/{token}/reply/voice",
        files={"audio": ("reply.wav", junk, "audio/wav")},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    assert "Unsupported+audio" in resp.headers["location"]
    # no audio bytes anywhere in the store
    for row in st.store.unhandled_responses():
        assert "RIFF" not in str(row) and b"RIFF" not in str(row).encode()


def test_responder_fallback_logged_not_configured(client):
    """Text reply without manual fields and without credentials → the agent
    falls back (logged to /debug/gcp) and the user is told to use buttons."""
    st = client.app.state
    from app.tokens import subject_token

    member = next(m for m in st.production.crew)
    token = subject_token(st.settings.app_secret, "crew", member.id, st.token_epoch)
    _publish_a_plan(client)
    resp = client.post(
        f"/c/{token}/reply",
        data={"free_text": "I am stuck in traffic, 30 minutes out"},
        follow_redirects=False,
    )
    assert resp.status_code == 303
    calls = st.store.recent_gcp_calls()
    assert any(c["kind"] == "responder" and c["ok"] == 0 for c in calls)

