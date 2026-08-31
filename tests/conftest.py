"""Shared fixtures: seed production, rulebook context, per-test settings."""
from __future__ import annotations

import base64
import shutil
import sys
import uuid
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings  # noqa: E402
from app.importers import load_production  # noqa: E402
from app.rulebook import RuleBookContext, load_rulebook  # noqa: E402


def basic_auth_header(username: str, password: str) -> str:
    """Default Authorization header for AD-authenticated test clients.

    (This environment's TestClient has no `auth=` constructor kwarg, so the
    header is set on the client after construction.)
    """
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


@pytest.fixture()
def tmp_path():
    """Override pytest's builtin tmp_path.

    Two harness-sandbox constraints: the plugin's basetemp lifecycle is denied,
    and tempfile.mkdtemp() creates restricted-ACL dirs the sandboxed process
    cannot write into. A plain mkdir'd unique directory works everywhere.
    """
    base = ROOT / ".tmp"
    base.mkdir(exist_ok=True)
    d = base / f"bd-{uuid.uuid4().hex[:10]}"
    d.mkdir()
    yield d
    shutil.rmtree(d, ignore_errors=True)


@pytest.fixture(scope="session")
def production():
    prod, _ = load_production(ROOT / "seed")
    return prod


@pytest.fixture(scope="session")
def travel_times():
    _, travel = load_production(ROOT / "seed")
    return travel


@pytest.fixture(scope="session")
def rbc(travel_times):
    return RuleBookContext(load_rulebook(), travel_times)


@pytest.fixture()
def settings():
    """Offline-safe settings: Gemini unconfigured, tmp database."""
    s = Settings()
    s.db_path = ROOT / ".tmp" / f"test-{id(s)}.db"
    s.app_secret = "test-secret"
    s.seed_dir = ROOT / "seed"
    s.project_id = ""  # force-unconfigured unless a test opts in
    s.api_key = ""
    s.use_vertexai = True
    s.replit_dev_domain = ""
    s.replit_domains = []
    s.ad_username = "ad"
    s.ad_password = "test-ad-password"
    yield s
    s.db_path.unlink(missing_ok=True)


@pytest.fixture()
def client(settings, production, rbc):
    from app.store import Store
    from app.web import create_app
    from fastapi.testclient import TestClient

    store = Store(settings.db_path)
    app = create_app(
        settings=settings,
        production=production,
        rulebook_ctx=rbc,
        store=store,
    )
    # AD routes require Basic auth; send valid credentials by default so
    # route behavior tests stay focused. Auth-specific tests build their
    # own client without credentials.
    with TestClient(app) as c:
        c.headers["Authorization"] = basic_auth_header(
            settings.ad_username, settings.ad_password
        )
        yield c
    store.close()  # release the sqlite file before settings teardown unlinks it
