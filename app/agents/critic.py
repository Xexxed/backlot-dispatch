"""Critic agent — recommends one sandbox recovery option; NEVER mutates anything.

One Gemini call over every option's comparison stats (wrap, moves, feasibility,
cost/penalty fields when present) → a schema-enforced recommendation. The
critic may only RECOMMEND: it cannot reorder scenes, flip ``is_feasible``, or
write plan state — the human approval gate stays the only decision path.

Failure contract (same shape as intake/editor): any transport/parse/low-
confidence problem raises ``FallbackRequired`` and the route uses the
deterministic fallback — the cheapest feasible option (cost-aware when cost
fields exist, fewest-moves otherwise).
"""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field

from pydantic import BaseModel, Field

from app.agents.intake import MIN_CONFIDENCE, _sampling_kwargs
from app.config import Settings
from app.store import Store


class _CriticOut(BaseModel):
    """Schema Gemini is forced to emit (response_schema)."""

    recommended_strategy: str = Field(description="Exact strategy id to recommend")
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    reasons: list[str] = Field(
        default_factory=list,
        description="1-3 short reasons grounded in the provided stats",
    )


@dataclass
class CriticRecommendation:
    recommended_strategy: str
    confidence: float
    reasons: list[str] = field(default_factory=list)
    source: str = "fallback"  # gemini | fallback


class FallbackRequired(Exception):
    """Raised when the deterministic fallback must take over; carries reason."""


def _option_line(stats: dict) -> str:
    parts = [
        f"strategy={stats.get('strategy')}",
        f"feasible={stats.get('is_feasible')}",
        f"issues={stats.get('error_count', 0)}",
        f"scenes_moved={stats.get('moves', 0)}",
        f"wrap={stats.get('wrap_hhmm')}",
        f"wrap_delta_min={stats.get('wrap_delta')}",
    ]
    if stats.get("cost_total") is not None:
        parts.append(f"cost_usd={stats['cost_total']}")
    if stats.get("penalty_meals") is not None:
        parts.append(f"penalty_meals={stats['penalty_meals']}")
    return ", ".join(str(p) for p in parts)


def _system_prompt() -> str:
    return (
        "You are the critic agent for a film-set recovery tool. You see the "
        "deterministic recovery options for one incident and recommend ONE.\n"
        "Rules:\n"
        "- recommend_strategy must be EXACTLY one of the given strategy ids.\n"
        "- Ground every reason in the provided stats; never invent facts.\n"
        "- You only recommend: you cannot change schedules, feasibility, or "
        "any stored state. The human 1st AD decides.\n"
        "- Output JSON only."
    )


def manual_recommendation(options: list[dict]) -> CriticRecommendation:
    """Deterministic fallback (also the manual-parity contract twin): the
    cheapest feasible option — cost-aware when the ledger is present, then
    fewest moves, then earliest wrap. Empty/absent input → first option."""
    feasible = [o for o in options if o.get("is_feasible")]
    pool = feasible or options
    if not pool:
        return CriticRecommendation("", 0.0, ["No options to evaluate."], "fallback")

    def sort_key(o: dict):
        cost = o.get("cost_total")
        return (
            0 if o.get("is_feasible") else 1,
            float(cost) if isinstance(cost, (int, float)) else float("inf"),
            o.get("wrap_delta", 0) or 0,
            o.get("moves", 0) or 0,
        )

    best = sorted(pool, key=sort_key)[0]
    why_bits = []
    if best.get("is_feasible"):
        why_bits.append("feasible — passes all hard-rule compliance checks")
    else:
        why_bits.append("fewest remaining issues in an infeasible set")
    if best.get("cost_total") is not None:
        why_bits.append(f"lowest projected cost (${best['cost_total']})")
    else:
        why_bits.append(f"earliest wrap ({best.get('wrap_hhmm')})")
    why_bits.append(f"{best.get('moves', 0)} scene move(s)")
    return CriticRecommendation(
        best.get("strategy", ""),
        1.0,
        why_bits,
        "fallback",
    )


def recommend(
    options: list[dict],
    settings: Settings,
    store: Store | None = None,
) -> CriticRecommendation:
    """Recommend one option; NEVER raises — falls back deterministically.

    ``options`` are serialize.option_stats dicts (cost fields optional).
    """
    if not settings.gemini_configured or len(options) < 2:
        if store:
            # Evidence-log the skipped call so /debug/gcp stays truthful
            # about why the critic fell back.
            store.log_gcp_call(
                "critic",
                settings.gemini_model,
                0,
                ok=False,
                meta={"reason": "not_configured" if not settings.gemini_configured else "fewer_than_two_options"},
            )
        return manual_recommendation(options)

    try:  # lazy import keeps offline dev/test runs dependency-light
        from google import genai
        from google.genai import types
    except Exception:
        return manual_recommendation(options)

    strategy_ids = [str(o.get("strategy")) for o in options]
    listing = "\n".join(f"- {_option_line(o)}" for o in options)
    prompt = (
        f"Recovery options for one incident:\n{listing}\n\n"
        "Recommend exactly one strategy id with short grounded reasons."
    )
    try:
        if settings.use_vertexai:
            client = genai.Client(
                enterprise=True,
                project=settings.project_id,
                location=settings.gemini_location,
            )
        else:
            client = genai.Client(api_key=settings.api_key)
        started = time.perf_counter()
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(
                system_instruction=_system_prompt(),
                response_mime_type="application/json",
                response_schema=_CriticOut,
                **_sampling_kwargs(settings),
            ),
        )
        latency_ms = int((time.perf_counter() - started) * 1000)
    except Exception as exc:
        if store:
            store.log_gcp_call(
                "critic", settings.gemini_model, 0, ok=False, meta={"error": str(exc)[:300]}
            )
        return manual_recommendation(options)

    raw_text = (response.text or "").strip()
    meta: dict = {"response_bytes": len(raw_text), "options": len(options)}
    parsed: _CriticOut | None = None
    parse_error = ""
    try:
        parsed = _CriticOut.model_validate(json.loads(raw_text))
    except Exception as exc:
        parse_error = str(exc)[:200]

    invalid = (
        parsed is None
        or parsed.confidence < MIN_CONFIDENCE
        or parsed.recommended_strategy not in strategy_ids
    )
    if invalid:
        if store:
            store.log_gcp_call(
                "critic",
                settings.gemini_model,
                latency_ms,
                ok=False,
                meta={
                    **meta,
                    "reason": "low_confidence_or_parse_error_or_unknown_strategy",
                    "detail": parse_error,
                },
            )
        return manual_recommendation(options)

    if store:
        store.log_gcp_call("critic", settings.gemini_model, latency_ms, ok=True, meta=meta)
    return CriticRecommendation(
        parsed.recommended_strategy,
        parsed.confidence,
        [r for r in parsed.reasons if r.strip()][:3],
        "gemini",
    )
