"""Hosted-server configuration read from the environment (PLAT-MCP-HOSTED-1 S1).

Every required variable is read once at startup and a missing one raises a
RuntimeError naming it — a hosted process must never fall back to a default
credential or to an implicit API URL.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field

# design.md §2: the client callback URLs FastMCP will honour. Data, not code, so a
# new MCP client host is a config change (MCP_ALLOWED_CLIENT_REDIRECTS) not a release.
DEFAULT_ALLOWED_CLIENT_REDIRECTS: tuple[str, ...] = (
    "https://claude.ai/api/mcp/auth_callback",
    "https://grok.com/connectors-oauth-exchange-code/",
    "https://chatgpt.com/connector_platform_oauth_redirect",
    "https://chatgpt.com/connector/oauth/*",
    "http://localhost:*",
    "http://127.0.0.1:*",
)

DEFAULT_HOSTED_PORT = 8012
DEFAULT_SMARTMEMORY_WEB_URL = "https://app.smartmemory.ai"

_TRUE_VALUES = {"1", "true", "yes", "on"}

_REQUIRED_VARS: tuple[tuple[str, str], ...] = (
    ("api_url", "SMARTMEMORY_API_URL"),
    ("public_base_url", "MCP_PUBLIC_BASE_URL"),
    ("clerk_domain", "CLERK_DOMAIN"),
    ("clerk_oauth_client_id", "CLERK_OAUTH_CLIENT_ID"),
    ("clerk_oauth_client_secret", "CLERK_OAUTH_CLIENT_SECRET"),
    ("jwt_signing_key", "MCP_JWT_SIGNING_KEY"),
    ("state_encryption_key", "MCP_STATE_ENCRYPTION_KEY"),
    ("redis_url", "MCP_REDIS_URL"),
)


@dataclass(frozen=True)
class HostedConfig:
    """Resolved hosted-mode settings."""

    api_url: str
    public_base_url: str
    clerk_domain: str
    clerk_oauth_client_id: str
    clerk_oauth_client_secret: str
    jwt_signing_key: str
    state_encryption_key: str
    redis_url: str
    allowed_client_redirects: list[str] = field(
        default_factory=lambda: list(DEFAULT_ALLOWED_CLIENT_REDIRECTS)
    )
    web_url: str = DEFAULT_SMARTMEMORY_WEB_URL
    hosted_port: int = DEFAULT_HOSTED_PORT
    trust_proxy: bool = False

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> HostedConfig:
        """Build the config from `env` (defaults to os.environ). Fails loud."""
        source = os.environ if env is None else env

        values: dict[str, object] = {}
        for attribute, name in _REQUIRED_VARS:
            raw = (source.get(name) or "").strip()
            if not raw:
                raise RuntimeError(
                    f"Hosted mode requires the {name} environment variable to be set."
                )
            values[attribute] = raw.rstrip("/") if attribute.endswith("url") else raw

        redirects_raw = (source.get("MCP_ALLOWED_CLIENT_REDIRECTS") or "").strip()
        if redirects_raw:
            redirects = [part.strip() for part in redirects_raw.split(",")]
            values["allowed_client_redirects"] = [part for part in redirects if part]
        else:
            values["allowed_client_redirects"] = list(DEFAULT_ALLOWED_CLIENT_REDIRECTS)

        values["web_url"] = (
            (source.get("SMARTMEMORY_WEB_URL") or DEFAULT_SMARTMEMORY_WEB_URL)
            .strip()
            .rstrip("/")
        )

        port_raw = (source.get("MCP_HOSTED_PORT") or "").strip()
        if port_raw:
            try:
                values["hosted_port"] = int(port_raw)
            except ValueError as exc:
                raise RuntimeError(
                    f"MCP_HOSTED_PORT must be an integer port number, got {port_raw!r}."
                ) from exc
        else:
            values["hosted_port"] = DEFAULT_HOSTED_PORT

        trust_raw = (source.get("MCP_TRUST_PROXY") or "").strip().lower()
        values["trust_proxy"] = trust_raw in _TRUE_VALUES

        return cls(**values)  # type: ignore[arg-type]
