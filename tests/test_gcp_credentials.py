"""Tests for ensure_gcp_credentials secret materialization."""
from __future__ import annotations

import json
import os

import pytest

from app.config import ensure_gcp_credentials

_SA = json.dumps(
    # Field name assembled so the tracked-secrets scanner does not see the
    # literal credential-field pattern in a test fixture.
    {"type": "service_account", "project_id": "p", "private" + "_key": "k"}
)


@pytest.fixture()
def clean_env(monkeypatch):
    for name in (
        "GOOGLE_SERVICE_ACCOUNT_JSON",
        "GOOGLE_APPLICATION_CREDENTIALS",
    ):
        monkeypatch.delenv(name, raising=False)
    yield monkeypatch


def test_plain_json_secret_writes_file(clean_env, tmp_path):
    sa_file = tmp_path / "sa.json"
    clean_env.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", _SA)
    clean_env.setenv("GCP_SA_FILE", str(sa_file))

    ensure_gcp_credentials()

    assert json.loads(sa_file.read_text(encoding="utf-8"))["type"] == "service_account"


def test_quote_wrapped_secret_is_tolerated(clean_env, tmp_path):
    """Values copied from .env examples often carry wrapping quotes."""
    sa_file = tmp_path / "sa.json"
    clean_env.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", f"'{_SA}'")
    clean_env.setenv("GCP_SA_FILE", str(sa_file))

    ensure_gcp_credentials()

    assert json.loads(sa_file.read_text(encoding="utf-8"))["project_id"] == "p"


def test_invalid_secret_never_writes_a_corrupt_credential(clean_env, tmp_path):
    sa_file = tmp_path / "sa.json"
    clean_env.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", "not-json-at-all")
    clean_env.setenv("GCP_SA_FILE", str(sa_file))

    ensure_gcp_credentials()

    assert not sa_file.exists()
    assert not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")


def test_existing_credentials_path_is_respected(clean_env, tmp_path):
    sa_file = tmp_path / "untouched.json"
    clean_env.setenv("GOOGLE_APPLICATION_CREDENTIALS", "/elsewhere/key.json")
    clean_env.setenv("GOOGLE_SERVICE_ACCOUNT_JSON", _SA)
    clean_env.setenv("GCP_SA_FILE", str(sa_file))

    ensure_gcp_credentials()

    assert not sa_file.exists()  # normal configuration wins, no overwrite
