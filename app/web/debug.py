"""Debug/evidence pages: Gemini runtime calls and configuration flags."""
from __future__ import annotations

import time

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse

router = APIRouter()


@router.get("/debug/gcp")
def gcp_log(request: Request, refresh: str = ""):
    st = request.app.state
    calls = st.store.recent_gcp_calls()
    refresh_message = {
        "success": "Connection check passed — Vertex AI responded successfully.",
        "failure": "Connection check failed — see the latest log entry for details.",
    }.get(refresh)
    return st.templates.TemplateResponse(
        request,
        "debug_gcp.html",
        {"calls": calls, "refresh_message": refresh_message, "refresh_status": refresh},
    )


def _check_gcp_connection(request: Request) -> bool:
    """Make a minimal model request and record the result in the GCP evidence log."""
    st = request.app.state
    settings, store = st.settings, st.store
    started = time.perf_counter()

    if not settings.gemini_configured:
        store.log_gcp_call(
            "health_check",
            settings.gemini_model,
            0,
            ok=False,
            meta={"probe": "manual_refresh", "reason": "not_configured"},
        )
        return False

    try:
        from google import genai

        if settings.use_vertexai:
            client = genai.Client(
                enterprise=True,
                project=settings.project_id,
                location=settings.gemini_location,
            )
        else:
            client = genai.Client(api_key=settings.api_key)

        response = client.models.generate_content(
            model=settings.gemini_model,
            contents="Connection health check. Reply with exactly: OK",
        )
        response_text = (response.text or "").strip()
        if not response_text:
            raise ValueError("empty response")

        latency_ms = int((time.perf_counter() - started) * 1000)
        store.log_gcp_call(
            "health_check",
            settings.gemini_model,
            latency_ms,
            ok=True,
            meta={"probe": "manual_refresh", "response_bytes": len(response_text)},
        )
        return True
    except Exception as exc:
        latency_ms = int((time.perf_counter() - started) * 1000)
        store.log_gcp_call(
            "health_check",
            settings.gemini_model,
            latency_ms,
            ok=False,
            meta={"probe": "manual_refresh", "error": str(exc)[:300]},
        )
        return False


@router.post("/debug/gcp/refresh")
def refresh_gcp(request: Request):
    ok = _check_gcp_connection(request)
    result = "success" if ok else "failure"
    return RedirectResponse(f"/debug/gcp?refresh={result}", status_code=303)


@router.get("/debug/status")
def status(request: Request):
    st = request.app.state
    s = st.settings
    return {
        "gemini_configured": s.gemini_configured,
        "use_vertexai": s.use_vertexai,
        "project_id_set": bool(s.project_id),
        "model": s.gemini_model,
        "production": {"id": st.production.id, "title": st.production.title},
        "scenes": len(st.production.scenes),
        "crew": len(st.production.crew),
        "cast": len(st.production.cast),
        "plans": len(st.store.list_plans(limit=100)),
    }
