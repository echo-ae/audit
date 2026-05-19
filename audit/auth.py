"""Auth setup for the Claude Code Agent SDK.

Claude Code's authentication-precedence list
(https://code.claude.com/docs/en/authentication#authentication-precedence)
is:

  1. Cloud provider credentials (Bedrock / Vertex / Foundry, when their
     respective `CLAUDE_CODE_USE_*` flag is set)
  2. ANTHROPIC_AUTH_TOKEN  (Bearer-token mode — used by LLM gateways
     like OpenRouter, custom proxies, etc.)
  3. ANTHROPIC_API_KEY      (the canonical metered Anthropic API)
  4. apiKeyHelper
  5. CLAUDE_CODE_OAUTH_TOKEN (long-lived subscription token)
  6. Subscription OAuth credentials from `claude login`

This module supports three modes, picked in this order:

  - **gateway**: `ANTHROPIC_BASE_URL` points away from anthropic.com AND
    `ANTHROPIC_AUTH_TOKEN` is set. Used for OpenRouter and similar.
    We leave those two env vars intact but still scrub `ANTHROPIC_API_KEY`
    (it'd outrank the gateway token).

  - **oauth_token**: `CLAUDE_CODE_OAUTH_TOKEN` is set (Pro/Max/Team/Enterprise
    subscription, ideal for CI). We scrub `ANTHROPIC_API_KEY` and
    `ANTHROPIC_AUTH_TOKEN` so they can't outrank the OAuth token.

  - **keychain_login**: `~/.claude/.credentials.json` exists from
    `claude login`. Same scrubbing as oauth_token.

Anything else raises AuthError.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass
class AuthStatus:
    auth_mode: str            # "gateway" | "oauth_token" | "keychain_login"
    api_key_scrubbed: bool
    auth_token_scrubbed: bool
    claude_cli_path: str | None
    claude_cli_version: str | None
    credentials_file: Path | None
    gateway_base_url: str | None
    gateway_model: str | None  # value of ANTHROPIC_MODEL if set, for display


class AuthError(RuntimeError):
    pass


CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"


def _is_gateway_base(url: str) -> bool:
    """A non-empty BASE_URL that doesn't point at canonical Anthropic
    counts as 'gateway mode'."""
    u = url.strip().lower()
    if not u:
        return False
    # Treat anything except api.anthropic.com / console.anthropic.com as gateway.
    return "anthropic.com" not in u


def configure_auth(env_file: Path | None = None) -> AuthStatus:
    """Load .env, decide auth mode, scrub conflicting env vars accordingly.

    Returns an AuthStatus describing what was picked. Raises AuthError if
    no usable auth path is available.
    """
    if env_file is not None and env_file.exists():
        load_dotenv(env_file)
    else:
        load_dotenv()

    cli_path = shutil.which("claude")
    if cli_path is None:
        raise AuthError(
            "`claude` CLI not found on PATH. Install Claude Code first: "
            "https://code.claude.com/docs/en/setup"
        )

    api_key_was_set = "ANTHROPIC_API_KEY" in os.environ
    base_url = os.environ.get("ANTHROPIC_BASE_URL", "")
    auth_token = os.environ.get("ANTHROPIC_AUTH_TOKEN", "").strip()
    gateway = _is_gateway_base(base_url) and bool(auth_token)

    auth_token_was_scrubbed = False
    if gateway:
        # Gateway path (OpenRouter / custom proxy / etc.): keep
        # ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN, but still drop
        # ANTHROPIC_API_KEY (rung 3 would outrank the gateway token).
        if api_key_was_set:
            del os.environ["ANTHROPIC_API_KEY"]
        mode = "gateway"
        creds_file = None
    else:
        # Subscription paths: scrub both API-key vars so subscription
        # OAuth wins precedence.
        if api_key_was_set:
            del os.environ["ANTHROPIC_API_KEY"]
        if "ANTHROPIC_AUTH_TOKEN" in os.environ:
            del os.environ["ANTHROPIC_AUTH_TOKEN"]
            auth_token_was_scrubbed = True

        token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip()
        creds_file = CREDENTIALS_PATH if CREDENTIALS_PATH.exists() else None
        if token:
            mode = "oauth_token"
        elif creds_file is not None:
            mode = "keychain_login"
        else:
            raise AuthError(
                "No auth available. Pick one of:\n"
                "  (a) Subscription OAuth (interactive): run `claude login`.\n"
                "  (b) Subscription OAuth (headless): run `claude setup-token` "
                "and paste into .env as CLAUDE_CODE_OAUTH_TOKEN.\n"
                "  (c) LLM gateway (OpenRouter / proxy): set "
                "ANTHROPIC_BASE_URL + ANTHROPIC_AUTH_TOKEN."
            )

    cli_version: str | None = None
    try:
        out = subprocess.run(
            [cli_path, "--version"], capture_output=True, text=True, timeout=10
        )
        if out.returncode == 0:
            cli_version = out.stdout.strip()
    except (subprocess.SubprocessError, OSError):
        pass

    return AuthStatus(
        auth_mode=mode,
        api_key_scrubbed=api_key_was_set,
        auth_token_scrubbed=auth_token_was_scrubbed,
        claude_cli_path=cli_path,
        claude_cli_version=cli_version,
        credentials_file=creds_file,
        gateway_base_url=base_url if mode == "gateway" else None,
        gateway_model=os.environ.get("ANTHROPIC_MODEL") if mode == "gateway" else None,
    )
