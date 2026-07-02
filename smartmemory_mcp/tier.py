"""Tier resolution for SmartMemory MCP server.

Determines capability tier (FREE / PRO / PRO_PLUS) from local credentials.
No network calls — resolution is purely local.
"""

from __future__ import annotations

import logging
import os
from enum import IntEnum
from pathlib import Path

logger = logging.getLogger(__name__)

_CONFIG_DIR = Path.home() / ".config" / "smartmemory"
_KEY_FILE = _CONFIG_DIR / ".api_key"


class Tier(IntEnum):
    """Capability tiers — higher value = more tools exposed."""

    FREE = 0
    PRO = 1
    PRO_PLUS = 2


def get_api_key() -> str:
    """Resolve API key from env var, keyring (via smartmemory_app), or file fallback."""

    # 1. Env var always wins
    env_key = os.environ.get("SMARTMEMORY_API_KEY", "").strip()
    if env_key:
        return env_key

    # 2. Try smartmemory_app keyring helper (desktop app installed)
    try:
        from smartmemory_app.config import get_api_key as _app_get_api_key

        app_key = _app_get_api_key()
        if app_key:
            return app_key
    except Exception as exc:
        logger.debug("smartmemory_app keyring lookup unavailable: %s", exc)

    # 3. File fallback
    try:
        if _KEY_FILE.exists():
            stat = _KEY_FILE.stat()
            if stat.st_mode & 0o077:
                logger.warning(
                    "API key file %s has overly permissive mode %o — reading anyway",
                    _KEY_FILE,
                    stat.st_mode & 0o777,
                )
            file_key = _KEY_FILE.read_text().strip()
            if file_key:
                return file_key
    except OSError as exc:
        logger.debug("Could not read API key file %s: %s", _KEY_FILE, exc)

    return ""


def store_api_key(key: str) -> None:
    """Persist API key via keyring (preferred) or file fallback with 0o600 permissions."""

    # 1. Try keyring via smartmemory_app
    try:
        import keyring

        keyring.set_password("smartmemory", "api_key", key)
        logger.debug("API key stored in keyring")
        return
    except Exception as exc:
        logger.debug("Keyring storage unavailable: %s", exc)

    # 2. File fallback
    _CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    _KEY_FILE.write_text(key)
    _KEY_FILE.chmod(0o600)
    logger.debug("API key stored at %s", _KEY_FILE)


def resolve_tier() -> Tier:
    """Determine which tools this MCP client *offers*, from local credentials.

    ADVISORY ONLY — this is not a security boundary. It decides which tools are
    registered/exposed in the local MCP client for UX (hide tools that would just
    fail), based on whether an API key is present and whether the operator opted
    into the full tool set. It performs no network validation and confers no
    entitlement: actual authorization is enforced server-side on every call
    (the hosted API re-checks the token's real plan per request). A user editing
    ``SMARTMEMORY_MCP_FULL_TOOLS`` only changes which local tools appear; a call
    to a tool they aren't entitled to still fails at the server.
    """

    api_key = get_api_key()

    if not api_key:
        return Tier.FREE

    # PRO_PLUS surfaces the full local tool set; opt-in via env var. Advisory only
    # (see docstring) — the server still enforces the caller's real entitlement.
    full_tools = os.environ.get("SMARTMEMORY_MCP_FULL_TOOLS", "").lower()
    if full_tools in ("true", "1"):
        return Tier.PRO_PLUS

    return Tier.PRO
