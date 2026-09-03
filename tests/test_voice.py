"""Phase 7 — voice-note intake: agent contract tests + route tests.

No network anywhere: the genai client is monkeypatched for the agent tests;
the route tests monkeypatch parse_incident_voice directly.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.agents import intake
from app.agents.intake import FallbackRequired, _VoiceIncidentOut, parse_incident_voice
from app.models import Incident
from app.store import Store
from app.web import routes_ad


def _wav_bytes(duration_s: float = 0.05) -> bytes:
    """Minimal valid 16 kHz 16-bit mono WAV (silence)."""
    frames = int(16000 * duration_s)
    pcm = b"\x00\x00" * frames
    return (
        b"RIFF" + (36 + len(pcm)).to_bytes(4, "little") + b"WAVE"
        + b"fmt " + (16).to_bytes(4, "little") + (1).to_bytes(2, "little")
        + (1).to_bytes(2, "little") + (16000).to_bytes(4, "little")
        + (32000).to_bytes(4, "little") + (2).to_bytes(2, "little")
        + (16).to_bytes(2, "little")
        + b"data" + len(pcm).to_bytes(4, "little") + pcm
    )


WAV = _wav_bytes()


# ------------------------------------------------------------- agent contract


def _fake_genai(monkeypatch, payload=None, error=None):
    """Replace genai.Client with a stub; returns the captured call kwargs."""
    from google import genai

    calls = []

    class FakeModels:
        def generate_content(self, **kwargs):
            calls.append(kwargs)
            if error is not None:
                raise error
            return SimpleNamespace(text=json.dumps(payload), usage_metadata=None)

    class FakeClient:
        def __init__(self, **kwargs):
            self.models = FakeModels()

    monkeypatch.setattr(genai, "Client", FakeClient)
    return calls


WELL_FORMED = {
    "type": "LOCATION_BLOCKED",
    "location_id": "Stage 4",
    "unit": "second unit",
    "blocked_until": "14:00",
    "severity": "high",
    "confidence": 0.9,
    "transcript": "Generator down at Stage 4, blocked until two PM.",
}


def test_voice_parse_success_maps_transcript_into_incident(
    settings, production, monkeypatch
):
    settings.project_id = "test-project"
    calls = _fake_genai(monkeypatch, payload=WELL_FORMED)

    incident = parse_incident_voice(WAV, settings, production.locations)

    assert incident.source == "voice"
    assert incident.free_text == WELL_FORMED["transcript"]
    assert incident.type == "LOCATION_BLOCKED"
    assert incident.location_id == "L-STAGE4"  # normalized from a spoken name
    assert incident.blocked_until == "14:00"
    assert incident.severity == "high"
    # one Gemini call carrying the audio bytes as an inline WAV part
    assert len(calls) == 1
    part = calls[0]["contents"][0]
    assert part.inline_data.data == WAV
    assert part.inline_data.mime_type == "audio/wav"


def test_voice_parse_low_confidence_falls_back(settings, production, monkeypatch):
    settings.project_id = "test-project"
    payload = dict(WELL_FORMED, confidence=0.3)
    _fake_genai(monkeypatch, payload=payload)

    with pytest.raises(FallbackRequired):
        parse_incident_voice(WAV, settings, production.locations)


def test_voice_parse_empty_transcript_falls_back(settings, production, monkeypatch):
    settings.project_id = "test-project"
    payload = dict(WELL_FORMED, transcript="   ")
    _fake_genai(monkeypatch, payload=payload)

    with pytest.raises(FallbackRequired):
        parse_incident_voice(WAV, settings, production.locations)


def test_voice_parse_transport_error_falls_back(settings, production, monkeypatch):
    settings.project_id = "test-project"
    _fake_genai(monkeypatch, error=RuntimeError("network down"))

    with pytest.raises(FallbackRequired) as excinfo:
        parse_incident_voice(WAV, settings, production.locations)
    assert "Gemini call failed" in excinfo.value.reason


def test_voice_parse_unconfigured_falls_back(settings, production):
    settings.project_id = ""
    with pytest.raises(FallbackRequired):
        parse_incident_voice(WAV, settings, production.locations)


def test_voice_calls_logged_without_transcript_text(
    settings, production, monkeypatch
):
    settings.project_id = "test-project"
    _fake_genai(monkeypatch, payload=WELL_FORMED)
    store = Store(settings.db_path)
    try:
        parse_incident_voice(WAV, settings, production.locations, store)

        rows = [r for r in store.recent_gcp_calls(limit=10) if r["kind"] == "intake_voice"]
        assert len(rows) == 1
        logged = rows[0]
        assert logged["ok"] == 1
        assert logged["model"] == settings.gemini_model
        meta = json.loads(logged["meta_json"])
        assert meta["audio_bytes"] == len(WAV)
        assert meta["mime"] == "audio/wav"
        assert meta["transcript_chars"] == len(WELL_FORMED["transcript"])
        assert WELL_FORMED["transcript"] not in logged["meta_json"]
    finally:
        store.close()


def test_voice_failure_also_logged(settings, production, monkeypatch):
    settings.project_id = "test-project"
    _fake_genai(monkeypatch, error=RuntimeError("boom"))
    store = Store(settings.db_path)
    try:
        with pytest.raises(FallbackRequired):
            parse_incident_voice(WAV, settings, production.locations, store)
        rows = [r for r in store.recent_gcp_calls(limit=10) if r["kind"] == "intake_voice"]
        assert len(rows) == 1
        assert rows[0]["ok"] == 0
        assert "boom" in rows[0]["meta_json"]
    finally:
        store.close()


def test_voice_schema_is_text_schema_plus_transcript():
    assert set(_VoiceIncidentOut.model_fields) == {
        *intake._IncidentOut.model_fields,
        "transcript",
    }


def test_confidence_gate_is_a_single_shared_constant():
    """Text and voice intake must never drift apart on the gating threshold."""
    assert intake.MIN_CONFIDENCE == 0.6


# ---------------------------------------------------------------- routes


def _post_voice(client, data: bytes, name: str = "note.wav", ctype: str = "audio/wav"):
    return client.post(
        "/incident/voice",
        files={"audio": (name, data, ctype)},
        follow_redirects=False,
    )


def _fake_incident(**overrides):
    base = dict(
        type="CAST_DELAY",
        location_id=None,
        severity="medium",
        free_text="Camera truck is thirty minutes out.",
        confidence=0.9,
        source="voice",
    )
    base.update(overrides)
    return Incident(**base)


def test_voice_route_success_nonblocking_goes_to_plan(client, monkeypatch):
    monkeypatch.setattr(
        routes_ad, "parse_incident_voice", lambda *a, **kw: _fake_incident()
    )
    response = _post_voice(client, WAV)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/plans/")

    plan_id = response.headers["location"].rsplit("/", 1)[-1]
    plan = client.app.state.store.get_plan(plan_id)
    assert plan["incident"]["source"] == "voice"
    assert plan["incident"]["free_text"] == "Camera truck is thirty minutes out."


def test_voice_route_success_blocking_goes_to_sandbox(client, monkeypatch):
    monkeypatch.setattr(
        routes_ad,
        "parse_incident_voice",
        lambda *a, **kw: _fake_incident(
            type="LOCATION_BLOCKED",
            location_id="L-STAGE4",
            blocked_until="14:00",
        ),
    )
    response = _post_voice(client, WAV)
    assert response.status_code == 303
    gid = response.headers["location"].rsplit("/", 1)[-1]
    assert response.headers["location"] == f"/sandbox/{gid}"
    assert len(client.app.state.store.plans_in_group(gid)) == 3


def test_voice_route_shows_transcript_on_review_page(client, monkeypatch):
    monkeypatch.setattr(
        routes_ad, "parse_incident_voice", lambda *a, **kw: _fake_incident()
    )
    response = _post_voice(client, WAV)
    plan_page = client.get(response.headers["location"])
    assert plan_page.status_code == 200
    assert "Voice note transcript" in plan_page.text
    assert "Camera truck is thirty minutes out." in plan_page.text


def test_voice_route_agent_fallback_rerenders_form(client, monkeypatch):
    def boom(*a, **kw):
        raise FallbackRequired("could not make it out")

    monkeypatch.setattr(routes_ad, "parse_incident_voice", boom)
    response = _post_voice(client, WAV)
    assert response.status_code == 200
    assert "handed off" in response.text
    assert "could not make it out" in response.text


def test_voice_route_rejects_empty_upload(client, monkeypatch):
    def explode(*a, **kw):  # must never be reached
        raise AssertionError("parse_incident_voice called on empty upload")

    monkeypatch.setattr(routes_ad, "parse_incident_voice", explode)
    response = _post_voice(client, b"")
    assert response.status_code == 200
    assert "No audio received" in response.text


def test_voice_route_rejects_oversize_upload(client, monkeypatch):
    def explode(*a, **kw):
        raise AssertionError("parse_incident_voice called on oversize upload")

    monkeypatch.setattr(routes_ad, "parse_incident_voice", explode)
    oversized = b"RIFF" + b"\x00" * routes_ad.MAX_VOICE_NOTE_BYTES
    response = _post_voice(client, oversized)
    assert response.status_code == 200
    assert "too long" in response.text


def test_voice_route_rejects_oversize_even_with_lying_content_length(
    client, monkeypatch
):
    """A body under the request-level Content-Length cap but over the WAV cap
    is caught by the capped read (cap+1 bytes), not trusted headers."""
    def explode(*a, **kw):
        raise AssertionError("parse_incident_voice called on oversize upload")

    monkeypatch.setattr(routes_ad, "parse_incident_voice", explode)
    # WAV cap + 1 byte file: total request stays within the 64 KiB multipart
    # margin, so the Content-Length pre-check passes and the read cap fires.
    just_over = b"R" * (routes_ad.MAX_VOICE_NOTE_BYTES + 1)
    response = _post_voice(client, just_over)
    assert response.status_code == 200
    assert "too long" in response.text


def test_voice_route_passes_completed_and_now_override(client, monkeypatch):
    """Dashboard form state must travel with the voice submission."""
    monkeypatch.setattr(
        routes_ad, "parse_incident_voice", lambda *a, **kw: _fake_incident()
    )
    response = client.post(
        "/incident/voice",
        files={"audio": ("note.wav", WAV, "audio/wav")},
        data={
            "completed": ["SC-101", "SC-102"],
            "now_override": "09:30",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    plan_id = response.headers["location"].rsplit("/", 1)[-1]
    plan = client.app.state.store.get_plan(plan_id)
    assert plan["completed_scene_ids"] == ["SC-101", "SC-102"]
    assert plan["now_minutes"] == 570  # 09:30


def test_voice_route_rejects_non_wav(client, monkeypatch):
    def explode(*a, **kw):
        raise AssertionError("parse_incident_voice called on non-WAV upload")

    monkeypatch.setattr(routes_ad, "parse_incident_voice", explode)
    response = _post_voice(client, b"this is definitely not a wav file payload")
    assert response.status_code == 200
    assert "Unsupported audio format" in response.text


def test_voice_route_without_file_field_rerenders_form(client):
    response = client.post("/incident/voice", data={}, follow_redirects=False)
    assert response.status_code == 200
    assert "No audio received" in response.text


def test_voice_note_capped_size_constant():
    """10 MB ≈ 5 min of 16 kHz 16-bit mono WAV — comfortably over the 2-min
    client cap so legit notes always pass."""
    five_minutes_wav = 16000 * 2 * 300
    assert routes_ad.MAX_VOICE_NOTE_BYTES >= five_minutes_wav
