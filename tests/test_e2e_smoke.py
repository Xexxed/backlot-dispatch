"""End-to-end smoke test: seeded production → incident → sandbox → publish → crew ack.

Mirrors the plan's validation gate (§6): assert changed call times land on a
personal link and the acknowledgment round-trips to the AD dashboard.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.tokens import subject_token
from app.web import create_app


def _post_incident(client: TestClient) -> str:
    response = client.post(
        "/incident",
        data={
            "free_text": "Generator down at Stage 4 until 14:00",
            "force_manual": "1",
            "manual_type": "LOCATION_BLOCKED",
            "manual_location": "L-STAGE4",
            "manual_blocked_until": "14:00",
            "manual_severity": "high",
            "completed": ["SC-101", "SC-102", "SC-103"],
            "now_override": "09:30",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text
    return response.headers["location"]


def test_full_incident_to_ack_roundtrip(client):
    # 1. dashboard renders
    home = client.get("/")
    assert home.status_code == 200
    assert "Report a disruption" in home.text

    # 2. blocking incident → what-if sandbox (three recovery options)
    location = _post_incident(client)
    assert location.startswith("/sandbox/")
    gid = location.rsplit("/", 1)[-1]

    sandbox = client.get(location)
    assert sandbox.status_code == 200
    assert "Minimal change" in sandbox.text
    assert "Cover-set pivot" in sandbox.text
    assert "Hold" in sandbox.text

    # 3. select a feasible option → review diff with narration
    store = client.app.state.store
    options = store.plans_in_group(gid)
    chosen = next((p for p in options if p["is_feasible"]), options[0])
    assert chosen["is_feasible"], "expected at least one feasible recovery option"
    selected = client.post(f"/plans/{chosen['id']}/select", follow_redirects=False)
    assert selected.status_code == 303
    diff = client.get(f"/plans/{chosen['id']}")
    assert diff.status_code == 200
    assert "Publish to crew" in diff.text
    assert "R-BLOCKED-LOCATION" in diff.text  # rule chips visible on diff
    assert chosen["strategy"] in diff.text

    # 4. publish → QR codes generated, dashboard announces live links
    published = client.post(f"/plans/{chosen['id']}/publish", follow_redirects=False)
    assert published.status_code == 303

    # 5. personal crew page reflects the new call time + changes
    token = subject_token("test-secret", "crew", "GE-09")  # generator tech
    card = client.get(f"/c/{token}")
    assert card.status_code == 200, card.text
    assert "What changed" in card.text
    assert "I've seen this" in card.text  # ack button present pre-ack
    assert f'action="/c/{token}/ack"' in card.text

    # 6. ack round-trip: POST then verify on the page and dashboard
    acked = client.post(f"/c/{token}/ack", data={}, follow_redirects=False)
    assert acked.status_code == 303
    after = client.get(f"/c/{token}")
    assert "Acknowledged" in after.text
    dash = client.get("/")
    assert "1/48" in dash.text  # 40 crew + 8 cast acknowledged count


def test_non_blocking_incident_goes_straight_to_plan(client):
    response = client.post(
        "/incident",
        data={
            "force_manual": "1",
            "manual_type": "CAST_DELAY",
            "manual_severity": "medium",
            "completed": ["SC-101", "SC-102"],
            "now_override": "10:00",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].startswith("/plans/")


def test_select_blocked_after_publish(client):
    gid = _post_incident(client).rsplit("/", 1)[-1]
    store = client.app.state.store
    plans = store.plans_in_group(gid)
    first = plans[0]
    client.post(f"/plans/{first['id']}/select")
    client.post(f"/plans/{first['id']}/publish")
    other = next(p for p in store.plans_in_group(gid) if p["id"] != first["id"])
    resp = client.post(f"/plans/{other['id']}/select", follow_redirects=False)
    assert resp.status_code == 303
    assert "already+published" in resp.headers["location"]


def test_unknown_token_404s(client):
    assert client.get("/c/not-a-real-token").status_code == 404


def test_fallback_form_renders_when_gemini_unconfigured(client, settings):
    settings.project_id = ""
    settings.api_key = ""
    response = client.post("/incident", data={"free_text": "Stage 4 blocked till noon"})
    assert response.status_code == 200
    assert "handed off" in response.text  # fallback banner with reason
