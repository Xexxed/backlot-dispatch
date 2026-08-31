"""Recovery timer: measured agent time persisted and surfaced honestly."""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.store import Store
from app.web import create_app


@pytest.fixture()
def env(settings, production, rbc):
    store = Store(settings.db_path)
    app = create_app(
        settings=settings,
        production=production,
        rulebook_ctx=rbc,
        store=store,
    )
    with TestClient(app) as c:
        yield c, store
    store.close()


def _post_manual_incident(client: TestClient) -> str:
    r = client.post(
        "/incident",
        data={
            "free_text": "Stage 4 blocked until 14:00",
            "force_manual": "1",
            "manual_type": "LOCATION_BLOCKED",
            "manual_location": "L-STAGE4",
            "manual_blocked_until": "14:00",
            "manual_severity": "high",
            "completed": ["SC-101"],
            "now_override": "09:30",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303, r.text
    gid = r.headers["location"].rsplit("/", 1)[-1]
    plan_id = client.app.state.store.plans_in_group(gid)[0]["id"]
    sel = client.post(f"/plans/{plan_id}/select", follow_redirects=False)
    assert sel.status_code == 303
    return plan_id


def test_plan_payload_records_recovery_seconds(env):
    client, store = env
    plan_id = _post_manual_incident(client)
    plan = store.get_plan(plan_id)
    value = plan.get("recovery_seconds")
    assert isinstance(value, (int, float))
    assert value >= 0


def test_plan_page_and_dashboard_surface_the_stat(env):
    client, _store = env
    plan_id = _post_manual_incident(client)

    diff = client.get(f"/plans/{plan_id}")
    assert diff.status_code == 200
    assert "Agent pipeline" in diff.text
    assert "Manual baseline" in diff.text
    assert "configurable industry assumption" in diff.text

    home = client.get("/")
    assert home.status_code == 200
    assert "Recovered latest plan" in home.text
    assert "manual baseline" in home.text


def test_recovery_stat_tolerates_legacy_plans(settings):
    """Plans stored before the field existed must not break any page."""
    from app.web.routes_ad import _recovery_stat

    assert _recovery_stat(None, settings) is None
    assert _recovery_stat({}, settings) is None
    assert _recovery_stat({"recovery_seconds": "oops"}, settings) is None
    assert _recovery_stat({"recovery_seconds": 0}, settings) is None

    stat = _recovery_stat({"recovery_seconds": 8.4}, settings)
    assert stat["display"] == "8.4s"
    assert "× faster" in stat["speedup_display"]
