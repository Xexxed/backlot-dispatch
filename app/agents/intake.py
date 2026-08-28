"""Intake Agent — Gemini structured incident parsing.

Runtime Google Cloud evidence for the contest gates:
  * Vertex AI (`google-genai`) call with response-schema enforcement
  * request/response metadata logged to the /debug/gcp evidence page
  * graceful fallback to the manual form on mis-parse, low confidence, or any
    transport failure — the deterministic core never depends on the model.
"""
from __future__ import annotations

import json
import re
import time

from pydantic import BaseModel, Field

from app.config import Settings
from app.models import INCIDENT_TYPES, SEVERITIES, Incident, Location
from app.store import Store

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


class FallbackRequired(Exception):
    """Raised when the manual form should take over; carries the reason."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class _IncidentOut(BaseModel):
    """Schema Gemini is forced to emit (response_schema)."""

    type: str = Field(description=f"One of: {', '.join(INCIDENT_TYPES)}")
    location_id: str | None = Field(
        default=None, description="Location id from the provided list, if affected"
    )
    unit: str | None = Field(default=None, description="Unit name if mentioned")
    blocked_until: str | None = Field(
        default=None, description="HH:MM 24h time until which the block lasts"
    )
    severity: str = Field(default="medium", description=f"One of: {', '.join(SEVERITIES)}")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


def _sampling_kwargs(settings: Settings, temperature: float = 0.0) -> dict:
    """Generation-config kwargs valid for both Gemini 2.5 and 3.x models.

    Gemini 3.x models reject/ignore temperature, top-P and top-K; reasoning
    effort is steered with thinking_level instead. Older models keep the
    temperature knob. Requires google-genai >= 2.20 for ThinkingConfig.
    """
    from google.genai import types  # lazy: keep offline runs dependency-light

    if settings.gemini_model.startswith("gemini-3"):
        return {
            "thinking_config": types.ThinkingConfig(
                thinking_level=settings.gemini_thinking_level
            )
        }
    return {"temperature": temperature}


def _system_prompt(locations: dict[str, Location]) -> str:
    loc_lines = "\n".join(f"- {loc.id}: {loc.name}" for loc in locations.values())
    return (
        "You are the intake agent for a film-set disruption tool. Convert the "
        "1st AD's incident report into JSON.\n"
        f"Known locations:\n{loc_lines}\n"
        "Rules:\n"
        "- Use EXACTLY one of the listed location ids when a location is "
        "affected; map spoken names like 'Stage Four' to the matching id.\n"
        "- blocked_until must be HH:MM 24-hour local time, or null if unknown.\n"
        "- confidence reflects how certain you are that type and location are "
        "correct. Output JSON only."
    )


def _normalize_location(raw: str | None, locations: dict[str, Location]) -> str | None:
    """Map free-text location mentions to known ids.

    Order of preference: exact id → id case-insensitive → substring either
    direction → word overlap (len>=4 words), e.g. "the ranch" → L-RANCH.
    """
    if not raw:
        return None
    cleaned_raw = raw.strip()
    if cleaned_raw in locations:
        return cleaned_raw

    def squash(value: str) -> str:
        return re.sub(r"[^a-z0-9 ]+", " ", value.lower())

    raw_squashed = squash(cleaned_raw)
    for loc_id in locations:
        if loc_id.lower() == cleaned_raw.lower():
            return loc_id
    token_index: dict[str, set[str]] = {}
    for loc_id, loc in locations.items():
        squashed_name = squash(loc.name)
        if raw_squashed and (raw_squashed in squashed_name or squashed_name in raw_squashed):
            return loc_id
        token_index[loc_id] = {w for w in squashed_name.split() if len(w) >= 4}
    raw_words = set(raw_squashed.split())
    best_id, best_score = None, 0
    for loc_id, tokens in token_index.items():
        score = len(tokens & raw_words)
        if score > best_score:
            best_id, best_score = loc_id, score
    return best_id


def _normalize_blocked_until(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip()
    m = _HHMM.match(value)
    if not m:
        return None
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def parse_incident(
    text: str,
    settings: Settings,
    locations: dict[str, Location],
    store: Store | None = None,
) -> Incident:
    """Parse free text into an Incident; raises FallbackRequired when the
    deterministic path must take over."""
    if not text.strip():
        raise FallbackRequired("Empty report — fill in the form instead.")
    if not settings.gemini_configured:
        raise FallbackRequired(
            "Gemini/Vertex credentials not configured — using the manual form."
        )

    try:  # lazy import keeps offline dev/test runs dependency-light
        from google import genai
        from google.genai import types
    except Exception as exc:  # pragma: no cover - environment-specific
        raise FallbackRequired(f"google-genai SDK unavailable: {exc}") from exc

    try:
        if settings.use_vertexai:
            # Gemini 3.x models resolve via the global endpoint (see
            # settings.gemini_location); enterprise=True selects the
            # Gemini Enterprise Agent Platform (formerly Vertex AI) API.
            client = genai.Client(
                enterprise=True,
                project=settings.project_id,
                location=settings.gemini_location,
            )
        else:
            client = genai.Client(api_key=settings.api_key)
        prompt = (
            "Incident report:\n\"\"\"\n" + text.strip() + "\n\"\"\"\n"
            "Return the structured incident JSON."
        )
        started = time.perf_counter()
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=_system_prompt(locations),
                response_mime_type="application/json",
                response_schema=_IncidentOut,
                **_sampling_kwargs(settings),
            ),
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
    except Exception as exc:
        if store:
            store.log_gcp_call(
                "intake", settings.gemini_model, 0, ok=False, meta={"error": str(exc)[:300]}
            )
        raise FallbackRequired(f"Gemini call failed: {exc}") from exc

    raw_text = (response.text or "").strip()
    meta: dict = {"response_bytes": len(raw_text)}
    usage = getattr(response, "usage_metadata", None)
    if usage is not None:
        meta["prompt_tokens"] = getattr(usage, "prompt_token_count", None)
        meta["output_tokens"] = getattr(usage, "candidates_token_count", None)

    parsed: _IncidentOut | None = None
    parse_error = ""
    try:
        data = json.loads(raw_text)
        parsed = _IncidentOut.model_validate(data)
    except Exception as exc:
        parse_error = str(exc)[:200]

    if parsed is None or parsed.confidence < 0.6:
        if store:
            store.log_gcp_call(
                "intake",
                settings.gemini_model,
                latency_ms,
                ok=False,
                meta={**meta, "reason": "low_confidence_or_parse_error", "detail": parse_error},
            )
        raise FallbackRequired(
            "Intake agent was not confident enough — confirm details on the form."
        )

    location_id = _normalize_location(parsed.location_id, locations)
    severity = parsed.severity if parsed.severity in SEVERITIES else "medium"
    itype = parsed.type if parsed.type in INCIDENT_TYPES else "OTHER"

    if store:
        store.log_gcp_call("intake", settings.gemini_model, latency_ms, ok=True, meta=meta)

    return Incident(
        type=itype,
        location_id=location_id,
        unit=parsed.unit,
        blocked_until=_normalize_blocked_until(parsed.blocked_until),
        severity=severity,
        free_text=text.strip(),
        confidence=parsed.confidence,
        source="gemini",
    )
