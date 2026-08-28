"""Editor agent — Gemini structured schedule-edit parsing.

The LLM converts the 1st AD's plain-language edit request into EditIntent
objects. It NEVER mutates the schedule: application is deterministic
(app/edit_ops.py) and the narration-only rule extends to edits. Graceful
fallback to the manual form on mis-parse, low confidence, or transport
failure — same contract as the intake agent.
"""
from __future__ import annotations

import json
import time

from pydantic import BaseModel, Field

from app.agents.intake import FallbackRequired, _normalize_location, _sampling_kwargs
from app.config import Settings
from app.models import EDIT_ACTIONS, EditIntent, Production
from app.store import Store


class _IntentOut(BaseModel):
    """Schema Gemini is forced to emit per intent (response_schema)."""

    action: str = Field(description=f"One of: {', '.join(EDIT_ACTIONS)}")
    scene_id: str | None = Field(
        default=None, description="Exact scene id to move or relocate"
    )
    ref_scene_id: str | None = Field(
        default=None,
        description="move/add placement: place the scene AFTER this scene id; null = end of day",
    )
    new_location_id: str | None = Field(
        default=None, description="swap_location: exact target location id"
    )
    title: str | None = Field(default=None, description="add_scene: short scene title")
    page_count: float = Field(
        default=1.0, gt=0, description="add_scene: script pages (~1 minute each)"
    )
    location_id: str | None = Field(
        default=None, description="add_scene: exact location id"
    )
    int_ext: str = Field(default="INT", description="add_scene: INT or EXT")
    day_night: str = Field(default="DAY", description="add_scene: DAY or NIGHT")


class _EditsOut(BaseModel):
    intents: list[_IntentOut] = Field(
        default_factory=list, description="0-3 edits, in the order requested"
    )
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


def _system_prompt(production: Production) -> str:
    scene_lines = "\n".join(
        f"- {sid}: {production.scenes[sid].title}" for sid in production.scene_order
    )
    loc_lines = "\n".join(
        f"- {loc.id}: {loc.name}" for loc in production.locations.values()
    )
    return (
        "You are the schedule-edit agent for a film-set tool. Convert the 1st "
        "AD's requested schedule change into structured edit intents.\n"
        f"Known scenes (ids are exact):\n{scene_lines}\n"
        f"Known locations:\n{loc_lines}\n"
        "Actions:\n"
        "- move_scene: reorder. scene_id = scene to move; ref_scene_id = place "
        "it AFTER that scene (null = end of the day).\n"
        "- swap_location: shoot an existing scene somewhere else. scene_id + "
        "new_location_id.\n"
        "- add_scene: new pickup/insert scene. location_id required; title, "
        "page_count, int_ext, day_night as stated (defaults INT/DAY).\n"
        "Rules:\n"
        "- Use EXACT ids from the lists; map spoken names ('the ranch', "
        "'Stage Four') to the matching ids.\n"
        "- Return 0-3 intents in the order the AD stated them; return an empty "
        "intents list when the text is not an edit request.\n"
        "- confidence reflects how certain you are the intents match the "
        "request. Output JSON only."
    )


def _clean_scene_id(raw: str | None) -> str | None:
    return raw.strip().upper() if raw and raw.strip() else None


def parse_edit_intents(
    text: str,
    settings: Settings,
    production: Production,
    store: Store | None = None,
) -> list[EditIntent]:
    """Parse free text into EditIntents; raises FallbackRequired when the
    manual form must take over."""
    if not text.strip():
        raise FallbackRequired("Empty request — describe the change or use the form.")
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
            "Edit request:\n\"\"\"\n" + text.strip() + "\n\"\"\"\n"
            "Return the structured edit intents JSON."
        )
        started = time.perf_counter()
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=_system_prompt(production),
                response_mime_type="application/json",
                response_schema=_EditsOut,
                **_sampling_kwargs(settings),
            ),
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
    except Exception as exc:
        if store:
            store.log_gcp_call(
                "editor", settings.gemini_model, 0, ok=False, meta={"error": str(exc)[:300]}
            )
        raise FallbackRequired(f"Gemini call failed: {exc}") from exc

    raw_text = (response.text or "").strip()
    meta: dict = {"response_bytes": len(raw_text)}
    usage = getattr(response, "usage_metadata", None)
    if usage is not None:
        meta["prompt_tokens"] = getattr(usage, "prompt_token_count", None)
        meta["output_tokens"] = getattr(usage, "candidates_token_count", None)

    parsed: _EditsOut | None = None
    parse_error = ""
    try:
        data = json.loads(raw_text)
        parsed = _EditsOut.model_validate(data)
    except Exception as exc:
        parse_error = str(exc)[:200]

    if parsed is None or parsed.confidence < 0.6 or not parsed.intents:
        if store:
            store.log_gcp_call(
                "editor",
                settings.gemini_model,
                latency_ms,
                ok=False,
                meta={
                    **meta,
                    "reason": "low_confidence_or_parse_error_or_empty",
                    "detail": parse_error,
                },
            )
        raise FallbackRequired(
            "Editor agent was not confident enough — build the edit on the form."
        )

    if store:
        store.log_gcp_call("editor", settings.gemini_model, latency_ms, ok=True, meta=meta)

    intents: list[EditIntent] = []
    for raw in parsed.intents[:3]:
        intents.append(
            EditIntent(
                action=raw.action if raw.action in EDIT_ACTIONS else "",
                scene_id=_clean_scene_id(raw.scene_id),
                ref_scene_id=_clean_scene_id(raw.ref_scene_id),
                new_location_id=_normalize_location(raw.new_location_id, production.locations),
                title=raw.title,
                page_count=float(raw.page_count),
                location_id=_normalize_location(raw.location_id, production.locations),
                int_ext=raw.int_ext.upper(),
                day_night=raw.day_night.upper(),
                confidence=parsed.confidence,
                source="gemini",
            )
        )
    return intents


def manual_edit_intent(
    action: str,
    scene_id: str = "",
    ref_scene_id: str = "",
    new_location_id: str = "",
    title: str = "",
    page_count: str = "1",
    location_id: str = "",
    int_ext: str = "INT",
    day_night: str = "DAY",
) -> EditIntent:
    """Build one intent from the manual fallback form fields."""
    try:
        pages = float(page_count)
    except ValueError:
        pages = 1.0
    return EditIntent(
        action=action,
        scene_id=_clean_scene_id(scene_id),
        ref_scene_id=_clean_scene_id(ref_scene_id) or None,
        new_location_id=new_location_id or None,
        title=title or None,
        page_count=pages if pages > 0 else 1.0,
        location_id=location_id or None,
        int_ext=int_ext,
        day_night=day_night,
        confidence=1.0,
        source="manual_form",
    )
