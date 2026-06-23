"""Auth setup for local Codex subscription runs.

This project intentionally uses Codex through the user's ChatGPT login. It
does not select OpenAI Platform API-key billing paths. The preflight checks the
local `codex` CLI because the Python SDK and CLI share cached Codex login
state.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class AuthStatus:
    auth_mode: str  # "chatgpt_subscription"
    codex_cli_path: str
    codex_cli_version: str | None
    login_status: str
    credentials_file: Path | None


class AuthError(RuntimeError):
    pass


CODEX_AUTH_PATH = Path.home() / ".codex" / "auth.json"


def configure_auth(env_file: Path | None = None) -> AuthStatus:
    """Load .env and verify Codex is signed in with ChatGPT.

    The function never reads credential contents. It only asks `codex login
    status` which authentication mode is active.
    """
    if env_file is not None and env_file.exists():
        load_dotenv(env_file)
    else:
        load_dotenv()

    cli_path = shutil.which("codex")
    if cli_path is None:
        raise AuthError(
            "`codex` CLI not found on PATH. Install Codex, then run "
            "`codex login` to sign in with ChatGPT."
        )

    cli_version = _codex_version(cli_path)
    login_status = _codex_login_status(cli_path)
    normalized = login_status.lower()

    if "api key" in normalized:
        raise AuthError(
            "Codex is logged in with an API key, but this audit build requires "
            "ChatGPT subscription auth. Run `codex logout`, then `codex login` "
            "and choose ChatGPT sign-in."
        )
    if "chatgpt" not in normalized:
        raise AuthError(
            "Could not confirm a ChatGPT-backed Codex login. Run `codex login` "
            "or `codex login --device-auth`, then retry `audit auth-check`."
        )

    return AuthStatus(
        auth_mode="chatgpt_subscription",
        codex_cli_path=cli_path,
        codex_cli_version=cli_version,
        login_status=login_status,
        credentials_file=CODEX_AUTH_PATH if CODEX_AUTH_PATH.exists() else None,
    )


def _codex_version(cli_path: str) -> str | None:
    try:
        out = subprocess.run(
            [cli_path, "--version"], capture_output=True, text=True, timeout=10
        )
    except (subprocess.SubprocessError, OSError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def _codex_login_status(cli_path: str) -> str:
    try:
        out = subprocess.run(
            [cli_path, "login", "status"],
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (subprocess.SubprocessError, OSError) as e:
        raise AuthError(
            f"Could not run `codex login status`: {e}. Run `codex login` and retry."
        ) from e

    text = "\n".join(part for part in (out.stdout, out.stderr) if part).strip()
    clean = _clean_status_text(text)
    if out.returncode != 0:
        detail = f" ({clean})" if clean else ""
        raise AuthError(
            "Codex is not logged in with ChatGPT. Run `codex login` or "
            f"`codex login --device-auth`, then retry.{detail}"
        )
    if not clean:
        raise AuthError(
            "Codex login status returned no usable status. Run `codex login`, "
            "then retry `audit auth-check`."
        )
    return clean


def _clean_status_text(text: str) -> str:
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("WARNING:"):
            continue
        lines.append(line)
    return lines[-1] if lines else ""
