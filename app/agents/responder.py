"""Crew Response Agent — inbound voice/text from a crew personal link.

One Gemini call (text or WAV audio) → schema-enforced ``CrewResponse``:
``kind`` (ack | running_late | cannot_make | constraint | question),
``eta_minutes``, ``leave_by`` (HH:MM), transcript, confidence.

Same contract as the intake agent (``app/agents/intake.py``):
  * ``FallbackRequired`` on transport failure / parse error / low confidence
    → the crew card shows the manual reply form (parity contract: the manual
    builder produces the same fields);
  * evidence logged to ``/debug/gcp`` as kind ``responder`` with
    ``audio_bytes`` + ``transcript_chars`` ONLY — the raw audio is never
    persisted and the personal narrative never enters any log;
  * the responder can only produce a structured reply — it never mutates
    schedules or plan state.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass

from pydantic import BaseModel, Field

from app.agents.intake import MIN_CONFIDENCE, _sampling_kwargs
from app.config import Settings
from app.store import Store

_HHMM = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")

RESPONSE_KINDS = ("ack", "running_late", "cannot_make", "constraint", "question")


class FallbackRequired(Exception):
    """Raised when the manual reply form must take over; carries the reason."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class _CrewResponseOut(BaseModel):
    """Schema Gemini is forced to emit (response_schema)."""

    kind: str = Field(description=f"One of: {', '.join(RESPONSE_KINDS)}")
    eta_minutes: int | None = Field(
        default=None, ge=0, description="running_late: minutes behind the published call"
    )
    leave_by: str | None = Field(
        default=None, description="cannot_make/constraint: HH:MM 24h time fact"
    )
    transcript: str = Field(default="", description="Verbatim text of the reply")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


@dataclass
class CrewResponse:
    kind: str
    eta_minutes: int | None
    leave_by: str | None
    transcript: str
    confidence: float
    source: str  # gemini | manual


def _system_prompt() -> str:
    return (
        "You classify a film crew member's reply about their call schedule.\n"
        "Rules:\n"
        "- kind must be EXACTLY one of: ack, running_late, cannot_make, "
        "constraint, question.\n"
        "- running_late: extract eta_minutes (whole minutes late) when stated.\n"
        "- cannot_make / constraint: extract leave_by as HH:MM 24-hour time "
        "when a time is stated.\n"
        "- Store the TIME FACT, not the story: do not restate personal "
        "details beyond the structured fields.\n"
        "- transcript is the verbatim reply text. Output JSON only."
    )


def _normalize_leave_by(raw: str | None) -> str | None:
    if not raw:
        return None
    value = raw.strip()
    m = _HHMM.match(value)
    if not m:
        return None
    return f"{int(m.group(1)):02d}:{m.group(2)}"


def manual_response(
    kind: str,
    free_text: str = "",
    eta_minutes: str = "",
    leave_by: str = "",
) -> CrewResponse:
    """Build a reply from the manual fallback form fields (parity twin of the
    Gemini path)."""
    kind_clean = kind if kind in RESPONSE_KINDS else "question"
    try:
        eta = max(int(eta_minutes), 0) if eta_minutes else None
    except ValueError:
        eta = None
    return CrewResponse(
        kind=kind_clean,
        eta_minutes=eta,
        leave_by=_normalize_leave_by(leave_by),
        transcript=(free_text or "").strip(),
        confidence=1.0,
        source="manual",
    )


def _classify(
    payload_builder,  # () -> (client, contents, config) once SDK imported
    settings: Settings,
    store: Store | None,
    kind_label: str,
    log_meta: dict,
) -> CrewResponse:
    """Shared Gemini round trip for text and voice replies."""
    try:
        from google import genai
        from google.genai import types
    except Exception as exc:  # pragma: no cover - environment-specific
        raise FallbackRequired(f"google-genai SDK unavailable: {exc}") from exc

    try:
        client, contents, config = payload_builder(genai, types)
        started = time.perf_counter()
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=contents,
            config=config,
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
    except Exception as exc:
        if store:
            store.log_gcp_call(
                kind_label,
                settings.gemini_model,
                0,
                ok=False,
                meta={**log_meta, "error": str(exc)[:300]},
            )
        raise FallbackRequired(f"Gemini call failed: {exc}") from exc

    raw_text = (response.text or "").strip()
    parsed: _CrewResponseOut | None = None
    parse_error = ""
    try:
        parsed = _CrewResponseOut.model_validate(json.loads(raw_text))
    except Exception as exc:
        parse_error = str(exc)[:200]

    transcript = (parsed.transcript or "").strip() if parsed else ""
    if (
        parsed is None
        or parsed.confidence < MIN_CONFIDENCE
        or parsed.kind not in RESPONSE_KINDS
        or (kind_label == "responder_voice" and not transcript)
    ):
        if store:
            store.log_gcp_call(
                kind_label,
                settings.gemini_model,
                latency_ms,
                ok=False,
                meta={
                    **log_meta,
                    "transcript_chars": len(transcript),
                    "reason": "low_confidence_or_parse_error",
                    "detail": parse_error,
                },
            )
        raise FallbackRequired(
            "The reply agent could not classify that — use the quick-reply buttons."
        )

    if store:
        store.log_gcp_call(
            kind_label,
            settings.gemini_model,
            latency_ms,
            ok=True,
            meta={**log_meta, "transcript_chars": len(transcript)},
        )

    return CrewResponse(
        kind=parsed.kind,
        eta_minutes=parsed.eta_minutes,
        leave_by=_normalize_leave_by(parsed.leave_by),
        transcript=transcript,
        confidence=parsed.confidence,
        source="gemini",
    )


def parse_reply(
    text: str,
    settings: Settings,
    store: Store | None = None,
) -> CrewResponse:
    """Classify an inbound text reply; raises FallbackRequired per contract."""
    if not text.strip():
        raise FallbackRequired("Empty reply — tap a quick-reply button instead.")
    if not settings.gemini_configured:
        if store:
            # Evidence-log the skipped call so /debug/gcp stays truthful.
            store.log_gcp_call(
                "responder",
                settings.gemini_model,
                0,
                ok=False,
                meta={"reason": "not_configured", "mode": "text"},
            )
        raise FallbackRequired(
            "Gemini/Vertex credentials not configured — using the quick-reply form."
        )

    def build(genai, types):
        if settings.use_vertexai:
            client = genai.Client(
                enterprise=True,
                project=settings.project_id,
                location=settings.gemini_location,
            )
        else:
            client = genai.Client(api_key=settings.api_key)
        contents = f"Crew member reply:\n\"\"\"\n{text.strip()}\n\"\"\"\nClassify it."
        config = types.GenerateContentConfig(
            system_instruction=_system_prompt(),
            response_mime_type="application/json",
            response_schema=_CrewResponseOut,
            **_sampling_kwargs(settings),
        )
        return client, contents, config

    return _classify(build, settings, store, "responder", {"mode": "text"})


def parse_reply_voice(
    audio: bytes,
    settings: Settings,
    store: Store | None = None,
) -> CrewResponse:
    """One Gemini call: WAV bytes → transcript + structured reply.

    Same contract as text; audio is processed in memory and never persisted
    (evidence logs carry lengths only)."""
    if not audio:
        raise FallbackRequired("Empty recording — record again or tap a button.")
    mime = "audio/wav"
    if not settings.gemini_configured:
        if store:
            store.log_gcp_call(
                "responder_voice",
                settings.gemini_model,
                0,
                ok=False,
                meta={"reason": "not_configured", "mime": mime},
            )
        raise FallbackRequired(
            "Gemini/Vertex credentials not configured — using the quick-reply form."
        )

    def build(genai, types):
        if settings.use_vertexai:
            client = genai.Client(
                enterprise=True,
                project=settings.project_id,
                location=settings.gemini_location,
            )
        else:
            client = genai.Client(api_key=settings.api_key)
        contents = [
            types.Part.from_bytes(data=audio, mime_type=mime),
            "The attached audio is a crew member's spoken reply about their "
            "call schedule. Transcribe it verbatim, then classify it.",
        ]
        config = types.GenerateContentConfig(
            system_instruction=_system_prompt(),
            response_mime_type="application/json",
            response_schema=_CrewResponseOut,
            **_sampling_kwargs(settings),
        )
        return client, contents, config

    return _classify(
        build, settings, store, "responder_voice", {"audio_bytes": len(audio), "mime": mime}
    )
