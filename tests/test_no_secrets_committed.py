"""Guardrail: private credentials must never exist in git-tracked files.

Defense against the ".replit / config drift" leak class: a service-account
key or signing secret pasted into a tracked file (e.g. .replit, a deploy
config) would ship with every clone and persist in history. This test scans
every tracked file for credential material and fails loudly instead.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

# Patterns that only ever appear inside real credential material — never in
# code, docs, or the commented placeholders in .env.example.
CREDENTIAL_PATTERNS = [
    "-----BEGIN RSA PRIVATE KEY-----",
    "-----BEGIN PRIVATE KEY-----",
    "-----BEGIN EC PRIVATE KEY-----",
    "-----BEGIN OPENSSH PRIVATE KEY-----",
    '"private_key"',  # field inside Google service-account JSON
    "PRIVATE KEY--------",  # minified single-line JSON variant
]


def _tracked_files() -> list[Path]:
    try:
        out = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=ROOT,
            capture_output=True,
            check=True,
        ).stdout.decode("utf-8")
    except (OSError, subprocess.CalledProcessError):  # no git in env
        pytest.skip("git unavailable; cannot enumerate tracked files")
    # Exclude this scanner itself: it necessarily contains the pattern literals.
    return [
        ROOT / name
        for name in out.split("\0")
        if name and name.replace("\\", "/") != "tests/test_no_secrets_committed.py"
    ]


def test_no_private_keys_or_service_accounts_in_tracked_files():
    offenders: list[str] = []
    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for pattern in CREDENTIAL_PATTERNS:
            if pattern in text:
                rel = path.relative_to(ROOT)
                offenders.append(f"{rel} contains {pattern!r}")
    assert not offenders, (
        "Credential material found in tracked files — move it to Replit "
        "Secrets / untracked .env, then rotate the exposed key:\n"
        + "\n".join(offenders)
    )


def test_app_secret_placeholder_not_a_real_value():
    """APP_SECRET= assignments in tracked files must be placeholders."""
    allowed_markers = ("change-me", "your-", "example", "<")
    for path in _tracked_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("#"):
                continue
            if stripped.startswith("APP_SECRET="):
                value = stripped.split("=", 1)[1].strip().strip("'\"")
                assert any(m in value.lower() for m in allowed_markers) or len(
                    value
                ) == 0, f"{path.name}: APP_SECRET looks like a real secret"
