"""CSRF defense for browser form posts: Origin/Referer host verification.

The app has no session cookies, but AD routes use HTTP Basic credentials,
which browsers attach automatically — so a hostile page could otherwise drive
publish/revert actions from the AD's browser.

Modern browsers always send an `Origin` header on cross-site POSTs, so for
mutating methods we verify the Origin (falling back to Referer) host against
the request Host (already vetted by TrustedHostMiddleware) and the configured
trusted-host allowlist. Requests without either header are non-browser
clients (curl, tests) and are allowed through; an attacker-controlled browser
request cannot suppress its Origin header. A literal `Origin: null`
(sandboxed iframes) has no host and is rejected.
"""
from __future__ import annotations

from urllib.parse import urlsplit

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.types import ASGIApp

_MUTATING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class OriginCSRFMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, trusted_hosts: list[str]) -> None:
        super().__init__(app)
        self._trusted = {h.lower() for h in trusted_hosts}

    async def dispatch(self, request: Request, call_next) -> Response:
        if request.method not in _MUTATING_METHODS:
            return await call_next(request)

        source = request.headers.get("origin") or request.headers.get("referer")
        if source is None:
            return await call_next(request)  # non-browser client

        source_host = urlsplit(source).netloc.lower()
        request_host = request.headers.get("host", "").lower()
        if not source_host or (
            source_host != request_host and source_host not in self._trusted
        ):
            return PlainTextResponse(
                "Cross-site form submission rejected.", status_code=403
            )
        return await call_next(request)
