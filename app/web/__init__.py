"""FastAPI application factory wiring state, templates, and routers."""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Settings, ensure_gcp_credentials, get_settings
from app.importers import load_production
from app.models import minutes_to_hhmm
from app.rulebook import RuleBookContext, load_rulebook
from app.store import Store
from app.tokens import build_token_index

APP_DIR = Path(__file__).resolve().parent.parent  # .../app
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"


def create_app(
    settings: Settings | None = None,
    production=None,
    travel_times: dict | None = None,
    rulebook_ctx: RuleBookContext | None = None,
    store: Store | None = None,
) -> FastAPI:
    ensure_gcp_credentials()  # materialize Replit secret → credentials file
    settings = settings or get_settings()
    if production is None:
        production, travel = load_production(settings.seed_dir)
    else:
        travel = travel_times or {}
    rbc = rulebook_ctx or RuleBookContext(load_rulebook(), travel)
    store = store or Store(settings.db_path)

    app = FastAPI(title="Backlot Dispatch", version="0.1.0")

    # Reject requests whose Host header is not on the allowlist *before* any
    # route runs. Generated artifacts (QR destinations) must never embed an
    # attacker-chosen authority; see Settings.trusted_hosts / external_base_url.
    from starlette.middleware.trustedhost import TrustedHostMiddleware

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)

    app.state.settings = settings
    app.state.production = production
    app.state.rbc = rbc
    app.state.store = store
    app.state.token_index = build_token_index(production, settings.app_secret)

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["hhmm"] = minutes_to_hhmm
    app.state.templates = templates

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    (STATIC_DIR / "qr").mkdir(parents=True, exist_ok=True)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from app.web.debug import router as debug_router
    from app.web.routes_ad import router as ad_router
    from app.web.routes_crew import router as crew_router

    app.include_router(ad_router)
    app.include_router(crew_router)
    app.include_router(debug_router)
    return app
