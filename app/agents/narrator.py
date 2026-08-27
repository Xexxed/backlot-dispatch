"""Narrator agent — plain-language crew comms.

The narrator may only DESCRIBE the change list produced by the deterministic
optimizer; it never alters schedule facts. If Gemini is unavailable, a
template-based summary (deterministic, always correct) is used instead.
"""
from __future__ import annotations

from app.config import Settings
from app.models import Change, Diagnostic
from app.store import Store

_SYSTEM = (
    "You write short, calm crew-facing schedule updates for a film set. "
    "Use ONLY the change list provided — never invent, reorder, or alter "
    "schedule facts. 120 words max, plain language, no jargon."
)


def template_summary(changes: list[Change], diagnostics: list[Diagnostic]) -> str:
    """Deterministic fallback: the change list itself, formatted."""
    lines = ["Schedule update from the AD team:"]
    for change in changes:
        if change.kind in ("MOVE", "MEAL", "INFO"):
            lines.append(f"• {change.reason}")
    warnings = [d.message for d in diagnostics if d.severity == "ERROR"]
    if warnings:
        lines.append("Needs AD attention:")
        lines.extend(f"• {w}" for w in warnings)
    return "\n".join(lines)


def narrate(
    changes: list[Change],
    diagnostics: list[Diagnostic],
    settings: Settings,
    store: Store | None = None,
) -> tuple[str, str]:
    """Returns (summary_text, source) where source is 'gemini' or 'template'."""
    fallback = template_summary(changes, diagnostics)
    if not settings.gemini_configured or not changes:
        return fallback, "template"

    try:
        from google import genai
        from google.genai import types
    except Exception:  # pragma: no cover
        return fallback, "template"

    change_lines = "\n".join(f"- [{c.rule_id}] {c.reason}" for c in changes)
    error_lines = "\n".join(f"- {d.message}" for d in diagnostics if d.severity == "ERROR")
    prompt = (
        f"Change list:\n{change_lines}\n"
        + (f"\nOpen issues flagged by compliance checks:\n{error_lines}" if error_lines else "")
        + "\n\nWrite the crew update."
    )
    try:
        if settings.use_vertexai:
            client = genai.Client(
                vertexai=True, project=settings.project_id, location="us-central1"
            )
        else:
            client = genai.Client(api_key=settings.api_key)
        started = __import__("time").perf_counter()
        response = client.models.generate_content(
            model=settings.gemini_model,
            contents=prompt,
            config=types.GenerateContentConfig(system_instruction=_SYSTEM, temperature=0.3),
        )
        latency_ms = int((__import__("time").perf_counter() - started) * 1000)
        text = (response.text or "").strip()
        if not text:
            raise ValueError("empty response")
        if store:
            store.log_gcp_call("narrator", settings.gemini_model, latency_ms, ok=True, meta={})
        return text, "gemini"
    except Exception as exc:
        if store:
            store.log_gcp_call(
                "narrator",
                settings.gemini_model,
                0,
                ok=False,
                meta={"error": str(exc)[:300]},
            )
        return fallback, "template"


def narrate_plan(
    plan: dict, settings: Settings, store: Store | None = None
) -> tuple[str, str]:
    """Narrate a stored plan payload (dicts) — reconstruct dataclasses first."""
    changes = [Change(**c) for c in plan.get("changes") or []]
    diagnostics = [Diagnostic(**d) for d in plan.get("diagnostics") or []]
    return narrate(changes, diagnostics, settings, store)
