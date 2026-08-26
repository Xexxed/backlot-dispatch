"""Deterministic per-person portal links.

Tokens are keyed HMACs of (secret, kind, subject_id) so they are stable across
restarts and reproducible in tests — no database of secrets required.
"""
from __future__ import annotations

import hashlib
import hmac


def subject_token(secret: str, kind: str, subject_id: str) -> str:
    digest = hmac.new(
        secret.encode("utf-8"), f"{kind}:{subject_id}".encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return digest[:20]


def build_token_index(production, secret: str) -> dict[str, tuple[str, str]]:
    """token -> (kind, subject_id) covering crew and cast."""
    index: dict[str, tuple[str, str]] = {}
    for member in production.crew:
        index[subject_token(secret, "crew", member.id)] = ("crew", member.id)
    for cast_id in production.cast:
        index[subject_token(secret, "cast", cast_id)] = ("cast", cast_id)
    return index


def lookup(index: dict[str, tuple[str, str]], token: str) -> tuple[str, str] | None:
    return index.get(token)
