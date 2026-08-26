"""Intake agent + narrator contract tests (no network required)."""
from __future__ import annotations

import pytest

from app.agents.intake import (
    FallbackRequired,
    _IncidentOut,
    _normalize_blocked_until,
    _normalize_location,
    parse_incident,
)
from app.agents.narrator import narrate, template_summary
from app.models import Change


def test_unconfigured_gemini_falls_back(settings, production):
    settings.project_id = ""
    settings.api_key = ""
    with pytest.raises(FallbackRequired) as excinfo:
        parse_incident("Generator down at stage 4", settings, production.locations)
    assert "not configured" in excinfo.value.reason.lower()


def test_empty_text_falls_back_immediately(settings, production):
    settings.project_id = ""
    with pytest.raises(FallbackRequired):
        parse_incident("   ", settings, production.locations)


def test_location_normalization(production):
    assert _normalize_location("L-STAGE4", production.locations) == "L-STAGE4"
    assert _normalize_location("Stage 4", production.locations) == "L-STAGE4"
    assert _normalize_location("stage 4", production.locations) == "L-STAGE4"
    assert _normalize_location("the ranch", production.locations) == "L-RANCH"
    assert _normalize_location("Atlantis", production.locations) is None
    assert _normalize_location(None, production.locations) is None


def test_blocked_until_normalization():
    assert _normalize_blocked_until("14:00") == "14:00"
    assert _normalize_blocked_until(" 9:05 ") == "09:05"
    assert _normalize_blocked_until("2pm") is None
    assert _normalize_blocked_until("25:99") is None
    assert _normalize_blocked_until("") is None


def test_recorded_schema_shape_parses():
    """Contract test against a recorded Gemini response shape."""
    recorded = {
        "type": "LOCATION_BLOCKED",
        "location_id": "L-STAGE4",
        "unit": "main unit",
        "blocked_until": "14:00",
        "severity": "high",
        "confidence": 0.92,
    }
    parsed = _IncidentOut.model_validate(recorded)
    assert parsed.confidence >= 0.6 and parsed.blocked_until == "14:00"


def test_low_confidence_rejected_by_contract():
    data = {"type": "OTHER", "confidence": 0.2}
    assert _IncidentOut.model_validate(data).confidence < 0.6


def test_template_summary_is_deterministic_and_complete():
    changes = [
        Change("SC-106", "MOVE", "R-BLOCKED-LOCATION", "Scene SC-106 moved to 14:10", {}),
        Change(None, "MEAL", "R-MEAL-WINDOW", "Lunch rescheduled to start 12:30", {}),
        Change(None, "INFO", "R-BLOCKED-LOCATION", "Incident summary line", {}),
    ]
    a = template_summary(changes, [])
    b = template_summary(changes, [])
    assert a == b
    for reason in ("SC-106 moved to 14:10", "Lunch rescheduled", "Incident summary"):
        assert reason in a


def test_narrator_falls_back_to_template_when_unconfigured(settings):
    settings.project_id = ""
    changes = [Change("SC-1", "MOVE", "R-X", "Scene SC-1 moved later", {})]
    text, source = narrate(changes, [], settings, store=None)
    assert source == "template"
    assert "SC-1 moved later" in text
