"""Tier resolution for SmartMemory MCP server.

Determines capability tier (FREE / PRO / PRO_PLUS) from local credentials.
No network calls — resolution is purely local.
"""

from __future__ import annotations

import logging
import os
import sys
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
    """Resolve environment, OS keyring, then the selected protected fallback."""
    if key := os.environ.get("SMARTMEMORY_API_KEY", "").strip():
        return key
    try:
        import keyring

        if key := keyring.get_password("smartmemory", "api_key"):
            return key
    except Exception as exc:
        logger.debug("Keyring lookup unavailable: %s", type(exc).__name__)

    if sys.platform == "win32":
        from smartmemory_mcp import windows_credentials

        try:
            return windows_credentials.read_key(
                windows_credentials.key_path(),
                legacy=windows_credentials.legacy_key_path(),
            )
        except OSError as exc:
            logger.warning("Protected credential unavailable: %s", exc)
            return ""

    try:
        from smartmemory_app.config import get_api_key as app_get_api_key

        if key := app_get_api_key():
            return key
    except ImportError:
        pass
    try:
        if _KEY_FILE.exists():
            if _KEY_FILE.stat().st_mode & 0o077:
                logger.warning("API key file %s has overly permissive mode", _KEY_FILE)
            return _KEY_FILE.read_text(encoding="utf-8").strip()
    except OSError as exc:
        logger.warning("Could not read API key file: %s", type(exc).__name__)
    return ""


def store_api_key(key: str) -> None:
    """Persist in keyring first. Windows consumers share one fallback writer."""
    try:
        import keyring

        keyring.set_password("smartmemory", "api_key", key)
        return
    except Exception as exc:
        logger.debug("Keyring storage unavailable: %s", type(exc).__name__)

    if sys.platform == "win32":
        from smartmemory_mcp import windows_credentials

        windows_credentials.store_key(
            windows_credentials.key_path(),
            key,
            legacy=windows_credentials.legacy_key_path(),
        )
        return

    _KEY_FILE.parent.mkdir(parents=True, exist_ok=True)
    _KEY_FILE.write_text(key, encoding="utf-8")
    _KEY_FILE.chmod(0o600)


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
