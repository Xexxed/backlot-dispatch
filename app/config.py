"""Application configuration loaded from environment variables.

Secrets are never committed: see .env.example. On Replit, use Secrets.
Locally, a .env file is loaded automatically (python-dotenv).
"""
from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import load_dotenv

# Load .env from the project root (no-op when absent / already in env)
load_dotenv(Path(__file__).resolve().parent.parent / ".env")


def _env(name: str, default: str = "") -> str:
    return os.getenv(name, default).strip()


def _hostname(value: str) -> str:
    """Return a TrustedHostMiddleware-compatible hostname.

    Deployment configuration should contain bare hostnames, but accepting an
    accidentally pasted origin here prevents a valid deployment from becoming
    unreachable while still ensuring the middleware never receives a URL.
    """
    candidate = value.strip().lower()
    if not candidate:
        return ""
    if "://" in candidate:
        candidate = urlsplit(candidate).hostname or ""
    else:
        candidate = candidate.split("/", 1)[0]
        if candidate.startswith("[") and "]" in candidate:
            candidate = candidate[1 : candidate.index("]")]
        elif candidate.count(":") == 1:
            candidate = candidate.rsplit(":", 1)[0]
    return candidate.strip().strip(".")


def ensure_gcp_credentials() -> None:
    """Replit-friendly credential loading.

    Replit Secrets are string key/values, but Vertex AI needs a service-account
    *file*. Accept the full JSON via the GOOGLE_SERVICE_ACCOUNT_JSON secret and
    materialize it to disk, pointing GOOGLE_APPLICATION_CREDENTIALS at it.
    No-op when credentials are already configured the normal way.
    """
    blob = _env("GOOGLE_SERVICE_ACCOUNT_JSON")
    if blob and not _env("GOOGLE_APPLICATION_CREDENTIALS"):
        path = Path(_env("GCP_SA_FILE", "instance/gcp-service-account.json"))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(blob, encoding="utf-8")
        os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = str(path)


class Settings:
    """Runtime settings; instantiate once at startup (see get_settings)."""

    def __init__(self) -> None:
        self.project_id = _env("GOOGLE_CLOUD_PROJECT")
        self.use_vertexai = _env("GOOGLE_GENAI_USE_VERTEXAI", "true").lower() == "true"
        self.api_key = _env("GOOGLE_API_KEY")
        self.gemini_model = _env("GEMINI_MODEL", "gemini-2.5-flash")
        self.app_secret = _env("APP_SECRET", "dev-secret-not-for-production")
        self.day_start = _env("DAY_START", "07:00")
        self.db_path = Path(_env("DB_PATH", "instance/backlot.db"))
        self.seed_dir = Path(_env("SEED_DIR", "seed"))
        # Canonical external origin for generated absolute links (QR payloads).
        # Never derive this from the request Host header: see trusted_hosts.
        self.public_base_url = _env("PUBLIC_BASE_URL").rstrip("/")
        self.replit_dev_domain = _env("REPLIT_DEV_DOMAIN")
        self.replit_domains = [
            d.strip().strip(".") for d in _env("REPLIT_DOMAINS").split(",") if d.strip()
        ]

    @property
    def running_on_replit(self) -> bool:
        """Heuristic Replit runtime detection (dev workspace or deployment)."""
        return bool(
            self.replit_dev_domain
            or self.replit_domains
            or _env("REPL_ID")
            or _env("REPLIT_ENVIRONMENT")
        )

    @property
    def external_base_url(self) -> str:
        """Configured canonical origin, or "" when unset (caller may fall back
        to the request base only because TrustedHostMiddleware validated it)."""
        if self.public_base_url:
            return self.public_base_url
        if self.replit_dev_domain:
            return f"https://{self.replit_dev_domain}"
        if self.replit_domains:
            return f"https://{self.replit_domains[0]}"
        return ""

    @property
    def trusted_hosts(self) -> list[str]:
        """Host-header allowlist for TrustedHostMiddleware.

        Local/dev names are always allowed; deployment origins come from
        TRUSTED_HOSTS (comma/space-separated), PUBLIC_BASE_URL, and Replit's
        injected domain env vars. When running on Replit but no explicit
        domain could be derived, fall back to *.replit.app/*.replit.dev so
        production stays reachable; set PUBLIC_BASE_URL/TRUSTED_HOSTS to pin
        the exact origin instead.
        """
        hosts = {"localhost", "127.0.0.1", "::1", "[::1]", "testserver"}
        extra = _env("TRUSTED_HOSTS")
        if extra:
            hosts.update(
                host
                for host in (_hostname(h) for h in extra.replace(",", " ").split())
                if host
            )
        for url in (self.public_base_url, self.external_base_url):
            if url:
                host = _hostname(url)
                if host:
                    hosts.add(host)
        if self.replit_dev_domain:
            host = _hostname(self.replit_dev_domain)
            if host:
                hosts.add(host)
        hosts.update(
            host
            for host in (_hostname(domain) for domain in self.replit_domains)
            if host
        )
        explicit = bool(
            extra or self.public_base_url or self.replit_dev_domain or self.replit_domains
        )
        if self.running_on_replit and not explicit:
            hosts.update({"*.replit.app", "*.replit.dev"})
        return sorted(hosts)

    @property
    def gemini_configured(self) -> bool:
        """True when enough credentials exist for a real Gemini/Vertex call."""
        if self.use_vertexai:
            return bool(self.project_id)
        return bool(self.api_key)


_SETTINGS: Settings | None = None


def get_settings() -> Settings:
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = Settings()
    return _SETTINGS


def set_settings(settings: Settings) -> None:
    """Test hook to swap settings deterministically."""
    global _SETTINGS
    _SETTINGS = settings
