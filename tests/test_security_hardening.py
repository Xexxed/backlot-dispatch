"""Security hardening regression tests.

Covers the four control layers added on top of Host-header validation:
AD Basic auth on console/debug routes, Origin-based CSRF checks on mutating
requests, refusal to boot on the dev-default APP_SECRET, and crew-link
expiry/rotation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.store import Store
from app.tokens import subject_token
from app.web import STATIC_DIR, create_app

AD_AUTH = ("ad", "test-ad-password")


@pytest.fixture()
def bare_client(settings, production, rbc):
    """Client WITHOUT AD credentials — uses the per-test settings/db."""
    store = Store(settings.db_path)
    app = create_app(
        settings=settings,
        production=production,
        rulebook_ctx=rbc,
        store=store,
    )
    with TestClient(app) as c:
        yield c
    store.close()


# --- AD Basic auth ----------------------------------------------------------


def test_ad_routes_require_credentials(bare_client):
    for method, path in (
        ("GET", "/"),
        ("GET", "/debug/gcp"),
        ("GET", "/debug/status"),
        ("POST", "/incident"),
    ):
        resp = bare_client.request(method, path)
        assert resp.status_code == 401, (method, path, resp.status_code)
        assert "www-authenticate" in resp.headers


def test_wrong_password_rejected(bare_client):
    resp = bare_client.get("/", auth=("ad", "nope"))
    assert resp.status_code == 401


def test_non_ascii_credentials_return_401_not_500(bare_client):
    """Latin-1-decoded Basic credentials must not crash compare_digest."""
    import base64

    header = base64.b64encode("ad:раdГугlK".encode("utf-8")).decode()
    resp = bare_client.get("/", headers={"Authorization": f"Basic {header}"})
    assert resp.status_code == 401


def test_ad_routes_503_when_password_unconfigured(settings, production, rbc):
    settings.ad_password = ""
    store = Store(settings.db_path)
    app = create_app(
        settings=settings, production=production, rulebook_ctx=rbc, store=store
    )
    with TestClient(app) as c:
        resp = c.get("/", auth=("ad", "anything"))
        assert resp.status_code == 503
    store.close()


def test_crew_portal_needs_no_ad_credentials(bare_client):
    """Personal links stay bearer-token only — crew have no AD password."""
    token = subject_token("test-secret", "crew", "GE-09")
    assert bare_client.get(f"/c/{token}").status_code == 200


# --- CSRF origin guard -------------------------------------------------------


def test_cross_origin_post_rejected_even_with_valid_auth(bare_client):
    resp = bare_client.post(
        "/incident",
        auth=AD_AUTH,
        headers={"Origin": "https://attacker.example"},
        data={"force_manual": "1", "manual_type": "OTHER"},
    )
    assert resp.status_code == 403


def test_null_origin_rejected(bare_client):
    resp = bare_client.post(
        "/incident",
        auth=AD_AUTH,
        headers={"Origin": "null"},
        data={"force_manual": "1", "manual_type": "OTHER"},
    )
    assert resp.status_code == 403


def test_same_origin_post_allowed(bare_client):
    resp = bare_client.post(
        "/incident",
        auth=AD_AUTH,
        headers={"Origin": "http://testserver"},
        data={"force_manual": "1", "manual_type": "OTHER"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


def test_originless_post_allowed_for_non_browser_clients(bare_client):
    resp = bare_client.post(
        "/incident",
        auth=AD_AUTH,
        data={"force_manual": "1", "manual_type": "OTHER"},
        follow_redirects=False,
    )
    assert resp.status_code == 303


# --- APP_SECRET enforcement ---------------------------------------------------


def test_env_startup_refuses_dev_default_secret(monkeypatch, tmp_path):
    import app.config as config

    monkeypatch.delenv("APP_SECRET", raising=False)
    monkeypatch.delenv("ALLOW_INSECURE_DEV_SECRET", raising=False)
    monkeypatch.setenv("DB_PATH", str(tmp_path / "sec.db"))
    monkeypatch.setattr(config, "_SETTINGS", None)
    with pytest.raises(RuntimeError, match="APP_SECRET"):
        create_app(settings=None)


def test_env_startup_allows_explicit_insecure_override(monkeypatch, tmp_path):
    from pathlib import Path

    import app.config as config

    monkeypatch.delenv("APP_SECRET", raising=False)
    monkeypatch.setenv("ALLOW_INSECURE_DEV_SECRET", "1")
    monkeypatch.setenv("DB_PATH", str(tmp_path / "sec.db"))
    monkeypatch.setenv("SEED_DIR", str(Path(__file__).resolve().parents[1] / "seed"))
    monkeypatch.setattr(config, "_SETTINGS", None)
    app = create_app(settings=None)
    assert app.title == "Backlot Dispatch"  # booted despite dev-default secret


# --- crew-link expiry & rotation ----------------------------------------------


def _publish_plan(client: TestClient) -> None:
    r = client.post(
        "/incident",
        data={
            "force_manual": "1",
            "manual_type": "CAST_DELAY",
            "manual_severity": "medium",
            "now_override": "10:00",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    plan_id = r.headers["location"].rsplit("/", 1)[-1]
    pub = client.post(f"/plans/{plan_id}/publish", follow_redirects=False)
    assert pub.status_code == 303


def test_links_expire_after_ttl(client, settings):
    token = subject_token("test-secret", "crew", "GE-09")
    assert client.get(f"/c/{token}").status_code == 200

    past = datetime.now(timezone.utc) - timedelta(
        hours=settings.token_ttl_hours + 1
    )
    client.app.state.store.set_token_issued_at(past.isoformat(timespec="seconds"))
    # Route reads the in-memory state captured at startup; mirror the restart.
    client.app.state.token_issued_at = past.isoformat(timespec="seconds")

    assert client.get(f"/c/{token}").status_code == 410
    assert (
        client.post(f"/c/{token}/ack", follow_redirects=False).status_code == 410
    )
    # The AD dashboard must surface the dead-link state, not the stale time.
    dash = client.get("/")
    assert dash.status_code == 200
    assert "links EXPIRED" in dash.text


def test_rotation_kills_old_links_and_regenerates_qr(client, settings):
    _publish_plan(client)
    old_token = subject_token("test-secret", "crew", "GE-09", epoch=0)
    assert client.get(f"/c/{old_token}").status_code == 200
    assert (STATIC_DIR / "qr" / f"{old_token}.svg").exists()

    resp = client.post("/links/rotate", follow_redirects=False)
    assert resp.status_code == 303

    assert client.get(f"/c/{old_token}").status_code == 404  # dead link
    assert not (STATIC_DIR / "qr" / f"{old_token}.svg").exists()

    new_token = subject_token("test-secret", "crew", "GE-09", epoch=1)
    assert client.get(f"/c/{new_token}").status_code == 200
    assert (STATIC_DIR / "qr" / f"{new_token}.svg").exists()


def test_acks_survive_rotation(client, settings):
    """Acks are recorded per subject, so reissued links keep the ack state."""
    _publish_plan(client)
    old_token = subject_token("test-secret", "crew", "GE-09", epoch=0)
    assert client.post(f"/c/{old_token}/ack", follow_redirects=False).status_code == 303

    client.post("/links/rotate", follow_redirects=False)
    new_token = subject_token("test-secret", "crew", "GE-09", epoch=1)
    card = client.get(f"/c/{new_token}")
    assert card.status_code == 200
    assert "Acknowledged" in card.text  # not re-prompted to ack again


def test_other_instance_rotation_is_observed_via_store(client, settings):
    """A rotation committed to the DB (e.g. by a second instance) must be
    honored by this process without it ever seeing /links/rotate."""
    client.post("/links/rotate", follow_redirects=False)  # epoch 1 live here
    # Simulate ANOTHER process rotating: bump the store behind this app's back.
    client.app.state.store.bump_token_epoch()
    epoch2_token = subject_token("test-secret", "crew", "GE-09", epoch=2)
    assert client.get(f"/c/{epoch2_token}").status_code == 200  # read-through works


def test_startup_reconciles_missing_qr_artifacts(client, settings, production, rbc):
    settings_fixture = client.app.state.settings
    settings_fixture.public_base_url = "https://backlot.example"
    _publish_plan(client)
    token = subject_token("test-secret", "crew", "GE-09", epoch=0)
    qr = STATIC_DIR / "qr" / f"{token}.svg"
    assert qr.exists()
    qr.unlink()

    from app.web import create_app

    create_app(
        settings=settings_fixture,
        production=production,
        rulebook_ctx=rbc,
        store=client.app.state.store,
    )
    assert qr.exists(), "startup must regenerate missing QR artifacts"


def test_epoch_zero_matches_pre_rotation_wire_format():
    """Links issued before the epoch feature deployed must keep working."""
    import hashlib
    import hmac

    legacy = hmac.new(
        b"test-secret", b"crew:GE-09", hashlib.sha256
    ).hexdigest()[:20]
    assert subject_token("test-secret", "crew", "GE-09", epoch=0) == legacy


def test_crew_portal_hides_ad_console_nav(bare_client):
    token = subject_token("test-secret", "crew", "GE-09")
    page = bare_client.get(f"/c/{token}")
    assert page.status_code == 200
    assert 'href="/changelog"' not in page.text  # AD links would 401 for crew


def test_rotation_resets_expiry(client, settings):
    past = datetime.now(timezone.utc) - timedelta(
        hours=settings.token_ttl_hours + 1
    )
    client.app.state.store.set_token_issued_at(past.isoformat(timespec="seconds"))
    client.app.state.token_issued_at = past.isoformat(timespec="seconds")
    old_token = subject_token("test-secret", "crew", "GE-09", epoch=0)
    assert client.get(f"/c/{old_token}").status_code == 410

    client.post("/links/rotate", follow_redirects=False)
    new_token = subject_token("test-secret", "crew", "GE-09", epoch=1)
    assert client.get(f"/c/{new_token}").status_code == 200
