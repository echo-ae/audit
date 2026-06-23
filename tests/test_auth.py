"""Codex subscription auth preflight tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from audit import auth as auth_mod
from audit.auth import AuthError, configure_auth


def _empty_env(tmp_path: Path) -> Path:
    p = tmp_path / ".env"
    p.write_text("")
    return p


def _fake_codex_cli(
    monkeypatch: pytest.MonkeyPatch,
    *,
    status_stdout: str = "Logged in using ChatGPT\n",
    status_stderr: str = "",
    status_returncode: int = 0,
) -> None:
    codex_path = "/usr/local/bin/codex"
    monkeypatch.setattr(auth_mod.shutil, "which", lambda name: codex_path if name == "codex" else None)

    def fake_run(cmd: list[str], **_: object) -> SimpleNamespace:
        if cmd == [codex_path, "--version"]:
            return SimpleNamespace(returncode=0, stdout="codex-cli 0.142.0-alpha.1\n", stderr="")
        if cmd == [codex_path, "login", "status"]:
            return SimpleNamespace(
                returncode=status_returncode,
                stdout=status_stdout,
                stderr=status_stderr,
            )
        raise AssertionError(f"unexpected command: {cmd!r}")

    monkeypatch.setattr(auth_mod.subprocess, "run", fake_run)


def test_missing_codex_cli(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(auth_mod.shutil, "which", lambda name: None)

    with pytest.raises(AuthError, match="codex.*CLI"):
        configure_auth(env_file=_empty_env(tmp_path))


def test_chatgpt_subscription_login_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_codex_cli(monkeypatch)
    monkeypatch.setattr(auth_mod, "CODEX_AUTH_PATH", tmp_path / "auth.json", raising=False)

    status = configure_auth(env_file=_empty_env(tmp_path))

    assert status.auth_mode == "chatgpt_subscription"
    assert status.codex_cli_path == "/usr/local/bin/codex"
    assert status.codex_cli_version == "codex-cli 0.142.0-alpha.1"
    assert status.login_status == "Logged in using ChatGPT"


def test_codex_api_key_login_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_codex_cli(monkeypatch, status_stdout="Logged in using API key\n")

    with pytest.raises(AuthError, match="ChatGPT"):
        configure_auth(env_file=_empty_env(tmp_path))


def test_missing_chatgpt_login_points_to_codex_login(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _fake_codex_cli(
        monkeypatch,
        status_stdout="",
        status_stderr="Not logged in\n",
        status_returncode=1,
    )

    with pytest.raises(AuthError, match="codex login"):
        configure_auth(env_file=_empty_env(tmp_path))


def test_openai_api_key_env_does_not_select_api_mode(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-should-not-be-used")
    _fake_codex_cli(monkeypatch)

    status = configure_auth(env_file=_empty_env(tmp_path))

    assert status.auth_mode == "chatgpt_subscription"
