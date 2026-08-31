"""FastAPI application factory wiring state, templates, and routers."""
from __future__ import annotations

from pathlib import Path

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.config import Settings, ensure_gcp_credentials, ensure_strong_secret, get_settings
from app.importers import load_production
from app.models import minutes_to_hhmm
from app.rulebook import RuleBookContext, load_rulebook
from app.store import Store
from app.tokens import build_token_index

APP_DIR = Path(__file__).resolve().parent.parent  # .../app
TEMPLATES_DIR = APP_DIR / "templates"
STATIC_DIR = APP_DIR / "static"


def _reconcile_qr_artifacts(state) -> None:
    """Regenerate any missing crew QR files from the persisted token epoch.

    QR payloads are derived state (secret + epoch + production), so they can
    always be rebuilt — this self-heals a crash mid-rotation, where the epoch
    bump committed but the SVG rewrite never finished. Skipped when no
    canonical origin is configured (nothing safe to encode yet).
    """
    base = state.settings.external_base_url
    if not base:
        return
    import segno  # local: startup path only

    for token in state.token_index:
        target = STATIC_DIR / "qr" / f"{token}.svg"
        if not target.exists():
            segno.make(f"{base}/c/{token}", error="m").save(
                str(target), kind="svg", scale=4, border=2
            )


def create_app(
    settings: Settings | None = None,
    production=None,
    travel_times: dict | None = None,
    rulebook_ctx: RuleBookContext | None = None,
    store: Store | None = None,
) -> FastAPI:
    ensure_gcp_credentials()  # materialize Replit secret → credentials file
    env_loaded = settings is None
    settings = settings or get_settings()
    if env_loaded:
        # Production startup path: never serve forgeable crew links.
        ensure_strong_secret(settings)
    if production is None:
        production, travel = load_production(settings.seed_dir)
    else:
        travel = travel_times or {}
    rbc = rulebook_ctx or RuleBookContext(load_rulebook(), travel)
    store = store or Store(settings.db_path)

    app = FastAPI(title="Backlot Dispatch", version="0.1.0")

    # Middleware run order: the LAST added runs FIRST. TrustedHost must be
    # outermost so the CSRF Origin check and route handlers only ever see a
    # validated Host header.
    from app.web.csrf import OriginCSRFMiddleware

    app.add_middleware(OriginCSRFMiddleware, trusted_hosts=settings.trusted_hosts)

    from starlette.middleware.trustedhost import TrustedHostMiddleware

    app.add_middleware(TrustedHostMiddleware, allowed_hosts=settings.trusted_hosts)

    app.state.settings = settings
    app.state.production = production
    app.state.rbc = rbc
    app.state.store = store
    # Crew-link epoch/issue time persist in SQLite, so restarts do not extend
    # expiry. Note: on Replit's ephemeral filesystem a redeploy starts a fresh
    # DB (epoch 0), which restores the original link set — redistribution of
    # links is part of any such redeploy after a rotation.
    epoch, issued_at = store.get_token_meta()
    app.state.token_epoch = epoch
    app.state.token_issued_at = issued_at
    app.state.token_index = build_token_index(production, settings.app_secret, epoch)

    templates = Jinja2Templates(directory=str(TEMPLATES_DIR))
    templates.env.filters["hhmm"] = minutes_to_hhmm
    app.state.templates = templates

    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    (STATIC_DIR / "qr").mkdir(parents=True, exist_ok=True)
    _reconcile_qr_artifacts(app.state)
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

    from app.web.auth import require_ad
    from app.web.debug import router as debug_router
    from app.web.routes_ad import router as ad_router
    from app.web.routes_crew import router as crew_router

    # AD console + debug evidence sit behind HTTP Basic auth. Crew portal
    # routes stay bearer-token only — the personal link is the credential.
    app.include_router(ad_router, dependencies=[Depends(require_ad)])
    app.include_router(crew_router)
    app.include_router(debug_router, dependencies=[Depends(require_ad)])
    return app
