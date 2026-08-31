"""AD-console authentication: HTTP Basic against AD_USERNAME / AD_PASSWORD.

Applied as a router-level dependency to the AD and debug routers in
app.web.create_app. Crew/cast portal routes stay unauthenticated on purpose —
their bearer token is the credential.

Fail-closed by design: with no AD_PASSWORD configured, AD routes answer 503
instead of falling back to open access.
"""
from __future__ import annotations

import secrets

from fastapi import Depends, HTTPException, Request
from fastapi.security import HTTPBasic, HTTPBasicCredentials

_scheme = HTTPBasic(auto_error=False)


async def require_ad(
    request: Request,
    credentials: HTTPBasicCredentials | None = Depends(_scheme),
) -> None:
    settings = request.app.state.settings
    if not settings.ad_password:
        raise HTTPException(
            status_code=503,
            detail="AD access is not configured on this deployment "
            "(set AD_PASSWORD).",
        )
    ok = False
    if credentials is not None:
        # compare_digest() on str requires ASCII; HTTPBasic decodes latin-1,
        # so a crafted header carrying non-ASCII chars would raise TypeError
        # (unauthenticated 500). Comparing UTF-8 bytes sidesteps it entirely.
        try:
            ok = secrets.compare_digest(
                credentials.username.encode("utf-8"),
                settings.ad_username.encode("utf-8"),
            ) and secrets.compare_digest(
                credentials.password.encode("utf-8"),
                settings.ad_password.encode("utf-8"),
            )
        except (TypeError, ValueError, UnicodeEncodeError):
            ok = False
    if not ok:
        raise HTTPException(
            status_code=401,
            detail="AD credentials required.",
            headers={"WWW-Authenticate": 'Basic realm="backlot-ad"'},
        )
