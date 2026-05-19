"""Auth setup tests — env scrubbing + the three auth modes."""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import pytest

from audit import auth as auth_mod
from audit.auth import AuthError, configure_auth


def _empty_env(tmp_path: Path) -> Path:
    p = tmp_path / ".env"
    p.write_text("")
    return p


def test_missing_everything_raises(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(auth_mod, "CREDENTIALS_PATH", tmp_path / "no_creds.json")
    with pytest.raises(AuthError, match="No auth available"):
        configure_auth(env_file=_empty_env(tmp_path))


def test_oauth_token_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-test-token")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-be-deleted")
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(auth_mod, "CREDENTIALS_PATH", tmp_path / "no_creds.json")
    if shutil.which("claude") is None:
        pytest.skip("claude CLI not installed")
    status = configure_auth(env_file=_empty_env(tmp_path))
    assert status.auth_mode == "oauth_token"
    assert status.api_key_scrubbed is True
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_keychain_mode(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_BASE_URL", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    creds = tmp_path / "creds.json"
    creds.write_text("{}")
    monkeypatch.setattr(auth_mod, "CREDENTIALS_PATH", creds)
    if shutil.which("claude") is None:
        pytest.skip("claude CLI not installed")
    status = configure_auth(env_file=_empty_env(tmp_path))
    assert status.auth_mode == "keychain_login"
    assert status.credentials_file == creds


def test_gateway_mode_openrouter(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """When ANTHROPIC_BASE_URL points at a non-anthropic host AND
    ANTHROPIC_AUTH_TOKEN is set, leave the gateway env intact and
    don't scrub the token."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://openrouter.ai/api")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "or-sk-xxx")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-be-deleted")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    if shutil.which("claude") is None:
        pytest.skip("claude CLI not installed")
    status = configure_auth(env_file=_empty_env(tmp_path))
    assert status.auth_mode == "gateway"
    assert status.gateway_base_url == "https://openrouter.ai/api"
    assert status.api_key_scrubbed is True
    assert status.auth_token_scrubbed is False
    # CRITICAL: the gateway token MUST still be in the env so the SDK can use it
    assert os.environ.get("ANTHROPIC_AUTH_TOKEN") == "or-sk-xxx"
    assert os.environ.get("ANTHROPIC_BASE_URL") == "https://openrouter.ai/api"
    assert "ANTHROPIC_API_KEY" not in os.environ


def test_gateway_mode_requires_both_url_and_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A base URL without a token doesn't trigger gateway mode."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://openrouter.ai/api")
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(auth_mod, "CREDENTIALS_PATH", tmp_path / "no_creds.json")
    if shutil.which("claude") is None:
        pytest.skip("claude CLI not installed")
    with pytest.raises(AuthError):
        configure_auth(env_file=_empty_env(tmp_path))


def test_anthropic_base_url_does_not_trigger_gateway(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A base URL pointing AT anthropic.com is normal — not gateway mode.
    Subscription scrubbing should still happen for the auth token."""
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "should-be-scrubbed")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-token")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    if shutil.which("claude") is None:
        pytest.skip("claude CLI not installed")
    status = configure_auth(env_file=_empty_env(tmp_path))
    assert status.auth_mode == "oauth_token"
    assert status.auth_token_scrubbed is True
    assert "ANTHROPIC_AUTH_TOKEN" not in os.environ


def test_missing_claude_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "fake-test-token")
    monkeypatch.setenv("PATH", "/nonexistent")
    with pytest.raises(AuthError, match="claude.*CLI"):
        configure_auth(env_file=_empty_env(tmp_path))
