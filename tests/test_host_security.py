"""Regression tests: Host-header poisoning of persistent QR destinations.

The publish route once built every QR payload from the raw request Host
header, so a single spoofed `Host: attacker.example` publish rewrote all
served QR SVGs to point at the attacker's site until the next publish.
Two layers now prevent this: TrustedHostMiddleware rejects unlisted hosts
before any route runs, and a configured PUBLIC_BASE_URL pins the canonical
origin regardless of request headers.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.store import Store
from app.tokens import subject_token
from app.web import STATIC_DIR, create_app
from tests.conftest import basic_auth_header


@pytest.fixture()
def client(settings, production, rbc):
    store = Store(settings.db_path)
    app = create_app(
        settings=settings,
        production=production,
        rulebook_ctx=rbc,
        store=store,
    )
    with TestClient(app) as c:
        c.headers["Authorization"] = basic_auth_header(
            settings.ad_username, settings.ad_password
        )
        yield c
    store.close()


def _make_plan(client: TestClient) -> str:
    """Blocking incident now routes to the sandbox; select an option and return
    the chosen plan id so the publish path under test still has a real plan."""
    response = client.post(
        "/incident",
        data={
            "free_text": "Generator down at Stage 4 until 14:00",
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
    assert response.status_code == 303, response.text
    gid = response.headers["location"].rsplit("/", 1)[-1]
    plan_id = client.app.state.store.plans_in_group(gid)[0]["id"]
    sel = client.post(f"/plans/{plan_id}/select", follow_redirects=False)
    assert sel.status_code == 303
    return plan_id


def _capture_segno_payloads(monkeypatch):
    """Record the exact strings segno encodes into QR codes."""
    import segno

    captured: list[str] = []
    real_make = segno.make

    def spy_make(data, *args, **kwargs):
        captured.append(str(data))
        return real_make(data, *args, **kwargs)

    monkeypatch.setattr(segno, "make", spy_make)
    return captured


def test_spoofed_host_header_rejected_everywhere(client):
    """Unlisted Host values must be refused before touching any route."""
    hostile = {"Host": "attacker.example"}
    assert client.get("/", headers=hostile).status_code == 400
    plan_id = _make_plan(client)  # plan creation itself used an allowed host
    assert (
        client.post(f"/plans/{plan_id}/publish", headers=hostile).status_code == 400
    )


def test_published_qr_uses_configured_origin_not_request_host(
    client, settings, monkeypatch
):
    """Even an allowed-but-different Host never leaks into the QR payload."""
    settings.public_base_url = "https://backlot.example"
    assert "backlot.example" in settings.trusted_hosts  # allowlist follows config

    captured = _capture_segno_payloads(monkeypatch)
    plan_id = _make_plan(client)
    published = client.post(f"/plans/{plan_id}/publish", follow_redirects=False)
    assert published.status_code == 303

    token = subject_token(settings.app_secret, "crew", "GE-09")
    qr_file = STATIC_DIR / "qr" / f"{token}.svg"
    assert qr_file.exists(), "publish must regenerate persistent QR assets"

    qr_urls = [p for p in captured if p.startswith("http")]
    assert qr_urls, "publish must encode absolute crew URLs"
    assert all(u.startswith("https://backlot.example/c/") for u in qr_urls)
    assert not any("testserver" in u for u in qr_urls)


def test_fallback_origin_is_validated_request_base(client, settings, monkeypatch):
    """Without PUBLIC_BASE_URL the validated request base is used."""
    captured = _capture_segno_payloads(monkeypatch)
    plan_id = _make_plan(client)
    assert client.post(
        f"/plans/{plan_id}/publish", follow_redirects=False
    ).status_code == 303
    qr_urls = [p for p in captured if p.startswith("http")]
    assert qr_urls
    assert all(u.startswith("http://testserver/c/") for u in qr_urls)


# --- allowlist derivation ---------------------------------------------------

_REPLIT_VARS = (
    "REPLIT_DEV_DOMAIN",
    "REPLIT_DOMAINS",
    "REPL_ID",
    "REPLIT_ENVIRONMENT",
    "TRUSTED_HOSTS",
    "PUBLIC_BASE_URL",
)


def _fresh_settings(monkeypatch, **env) -> "Settings":
    from app.config import Settings

    for name in _REPLIT_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return Settings()


def test_replit_deployment_domains_are_allowlisted(monkeypatch):
    s = _fresh_settings(monkeypatch, REPLIT_DOMAINS="a.replit.app, b.replit.app")
    assert s.external_base_url == "https://a.replit.app"
    hosts = s.trusted_hosts
    assert "a.replit.app" in hosts and "b.replit.app" in hosts
    assert not any(h.startswith("*") for h in hosts)  # explicit domains: no fallback


def test_on_replit_without_domains_falls_back_to_wildcards(monkeypatch):
    s = _fresh_settings(monkeypatch, REPL_ID="abc123")
    assert s.running_on_replit
    assert "*.replit.app" in s.trusted_hosts
    assert "*.replit.dev" in s.trusted_hosts
    assert s.external_base_url == ""


def test_off_replit_defaults_stay_tight(monkeypatch):
    s = _fresh_settings(monkeypatch)
    assert not s.running_on_replit
    assert not any("*" in h for h in s.trusted_hosts)
    assert "localhost" in s.trusted_hosts and "testserver" in s.trusted_hosts


def test_trusted_hosts_env_accepts_space_and_comma_separation(monkeypatch):
    s = _fresh_settings(monkeypatch, TRUSTED_HOSTS="foo.example, bar.example")
    assert {"foo.example", "bar.example"} <= set(s.trusted_hosts)


def test_trusted_hosts_env_normalizes_an_accidentally_pasted_origin(monkeypatch):
    s = _fresh_settings(monkeypatch, TRUSTED_HOSTS="https://foo.example/path")
    assert "foo.example" in s.trusted_hosts
    assert not any(host.startswith("https://") for host in s.trusted_hosts)
