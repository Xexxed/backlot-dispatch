"""Per-person portal links: HMAC tokens with expiry and one-shot rotation.

Tokens are keyed HMACs of (secret, epoch, kind, subject_id), so they stay
stable across restarts and reproducible in tests. Two operational controls
sit on top, with state persisted in the store (see Store.get_token_meta):

- Expiry: tokens are issued at a recorded time and stop working after
  TOKEN_TTL_HOURS (0/disabled → links never expire).
- Rotation: bumping the epoch changes every token, instantly invalidating
  all previously distributed links and QR codes.
"""
from __future__ import annotations

import hashlib
import hmac
from datetime import datetime, timedelta, timezone


def subject_token(secret: str, kind: str, subject_id: str, epoch: int = 0) -> str:
    # Epoch 0 keeps the pre-rotation wire format so existing links survive a
    # deploy of the epoch feature itself; the epoch enters the HMAC only once
    # a rotation has actually happened.
    message = (
        f"{kind}:{subject_id}" if epoch == 0 else f"{epoch}:{kind}:{subject_id}"
    )
    digest = hmac.new(
        secret.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return digest[:20]


def build_token_index(production, secret: str, epoch: int = 0) -> dict[str, tuple[str, str]]:
    """token -> (kind, subject_id) covering crew and cast."""
    index: dict[str, tuple[str, str]] = {}
    for member in production.crew:
        index[subject_token(secret, "crew", member.id, epoch)] = ("crew", member.id)
    for cast_id in production.cast:
        index[subject_token(secret, "cast", cast_id, epoch)] = ("cast", cast_id)
    return index


def lookup(index: dict[str, tuple[str, str]], token: str) -> tuple[str, str] | None:
    return index.get(token)


def sync_token_state(state) -> None:
    """Converge this process's in-memory token epoch/index with the store.

    The persisted store is the source of truth; re-reading per request lets
    every instance observe another instance's rotation instead of trusting a
    stale app.state copy.
    """
    epoch, issued_at = state.store.get_token_meta()
    if epoch != state.token_epoch:
        state.token_epoch = epoch
        state.token_index = build_token_index(
            state.production, state.settings.app_secret, epoch
        )
    state.token_issued_at = issued_at


def links_expire_at(issued_at_iso: str, ttl_hours: float) -> datetime | None:
    """Absolute expiry moment for the current link set; None = never expires."""
    if ttl_hours <= 0:
        return None
    return datetime.fromisoformat(issued_at_iso) + timedelta(hours=ttl_hours)


def links_valid(issued_at_iso: str | None, ttl_hours: float) -> bool:
    """True while the current token epoch is within its TTL."""
    if not issued_at_iso or ttl_hours <= 0:
        return True
    return datetime.now(timezone.utc) < links_expire_at(issued_at_iso, ttl_hours)
