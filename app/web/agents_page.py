"""Agent Control Room: /agents shows each pipeline run as an ordered trace.

``/debug/gcp`` stays the receipt (raw API call evidence); ``/agents`` is the
story — the multi-step pipeline made visible. AD-auth only (mounted with the
AD router, which carries the ``require_ad`` dependency).
"""
from __future__ import annotations

from fastapi import APIRouter, Request

router = APIRouter()


def _display_run(run: dict, steps: list[dict]) -> dict:
    """View-model: run + its steps with display-friendly fields."""
    duration = run.get("duration_ms")
    run_view = {
        "id": run["id"],
        "trigger": run.get("trigger") or "—",
        "status": run.get("status") or "running",
        "started_at": (run.get("started_at") or "")[:19].replace("T", " "),
        "duration_display": f"{duration} ms" if isinstance(duration, int) else "—",
        "steps": [
            {
                "seq": s["seq"],
                "name": s["name"],
                "agent": s["agent"] or "—",
                "kind": s["kind"],
                "model": s["model"] or "",
                "duration_display": (
                    f"{s['duration_ms']} ms" if s.get("duration_ms") is not None else "—"
                ),
                "status": s["status"],
                "verdict": s["verdict"] or "",
                "summary": s["summary"] or "",
            }
            for s in steps
        ],
    }
    return run_view


@router.get("/agents")
def agents(request: Request):
    st = request.app.state
    runs = st.store.recent_runs(limit=20)
    views = [_display_run(run, st.store.steps_for_run(run["id"])) for run in runs]
    return st.templates.TemplateResponse(
        request,
        "agents.html",
        {"runs": views},
    )
