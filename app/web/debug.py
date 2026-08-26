"""Debug/evidence pages: Gemini runtime calls and configuration flags."""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


@router.get("/debug/gcp")
def gcp_log(request: Request):
    st = request.app.state
    calls = st.store.recent_gcp_calls()
    return st.templates.TemplateResponse(
        request, "debug_gcp.html", {"calls": calls}
    )


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
