"""Plan versioning + one-click rollback.

Lifecycle: publish A (live) → publish B (A becomes superseded) → revert to A
(A live again, B superseded). Crew links are token-based and read the live
published plan dynamically, so rollback is instant.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


def _post_incident(client: TestClient) -> str:
    """Blocking incident → sandbox group id."""
    r = client.post(
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
    assert r.status_code == 303, r.text
    return r.headers["location"].rsplit("/", 1)[-1]


def _select_and_publish(client: TestClient, gid: str, strategy: str) -> str:
    """Select one sandbox option and publish it; returns its plan id."""
    store = client.app.state.store
    plan = next(p for p in store.plans_in_group(gid) if p["strategy"] == strategy)
    sel = client.post(f"/plans/{plan['id']}/select", follow_redirects=False)
    assert sel.status_code == 303
    pub = client.post(f"/plans/{plan['id']}/publish", follow_redirects=False)
    assert pub.status_code == 303
    return plan["id"]


def test_publish_supersedes_previous_and_revert_restores(client):
    gid = _post_incident(client)
    store = client.app.state.store

    # 1. Publish minimal → live.
    plan_a = _select_and_publish(client, gid, "minimal")
    live = store.latest_published_plan()
    assert live["id"] == plan_a
    assert store.previous_published_plan() is None  # nothing to revert to yet

    # 2. Publish cover_set → minimal becomes the rollback target.
    plan_b = _select_and_publish(client, gid, "cover_set")
    live = store.latest_published_plan()
    assert live["id"] == plan_b
    prior = store.previous_published_plan()
    assert prior is not None and prior["id"] == plan_a
    assert prior["status"] == "superseded"

    # 3. One-click revert → plan A is live again, B superseded.
    rev = client.post(f"/plans/{plan_a}/revert", follow_redirects=False)
    assert rev.status_code == 303
    live = store.latest_published_plan()
    assert live["id"] == plan_a
    prior = store.previous_published_plan()
    assert prior is not None and prior["id"] == plan_b

    # 4. Dashboard surfaces the revert affordance for the new prior (plan B).
    dash = client.get("/")
    assert dash.status_code == 200
    assert f"/plans/{plan_b}/revert" in dash.text


def test_revert_is_idempotent_for_live_plan(client):
    gid = _post_incident(client)
    store = client.app.state.store
    plan_a = _select_and_publish(client, gid, "minimal")
    rev = client.post(f"/plans/{plan_a}/revert", follow_redirects=False)
    assert rev.status_code == 303
    assert "already+the+live+plan" in rev.headers["location"]
    assert store.latest_published_plan()["id"] == plan_a


def test_revert_updates_crew_view(client):
    """After rollback the crew portal serves the restored plan and the
    dashboard banner names it as live."""
    from app.tokens import subject_token

    gid = _post_incident(client)
    token = subject_token("test-secret", "crew", "GE-09")

    plan_a = _select_and_publish(client, gid, "minimal")
    _select_and_publish(client, gid, "cover_set")

    client.post(f"/plans/{plan_a}/revert", follow_redirects=False)

    card = client.get(f"/c/{token}")
    assert card.status_code == 200
    assert "I've seen this" in card.text  # portal healthy on the restored plan

    dash = client.get("/")
    assert f"Live plan:</strong> {plan_a}" in dash.text
