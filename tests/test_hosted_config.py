"""HostedConfig env loading (PLAT-MCP-HOSTED-1 S1)."""

from __future__ import annotations

import pytest

from smartmemory_mcp.hosted.config import (
    DEFAULT_ALLOWED_CLIENT_REDIRECTS,
    HostedConfig,
)

REQUIRED_ENV = {
    "SMARTMEMORY_API_URL": "https://api.test",
    "MCP_PUBLIC_BASE_URL": "https://mcp.test",
    "CLERK_DOMAIN": "clerk.test",
    "CLERK_OAUTH_CLIENT_ID": "client-id",
    "CLERK_OAUTH_CLIENT_SECRET": "client-secret",
    "MCP_JWT_SIGNING_KEY": "signing-key",
    "MCP_STATE_ENCRYPTION_KEY": "fernet-key",
    "MCP_REDIS_URL": "redis://localhost:6379/0",
}


def _apply(monkeypatch, env: dict[str, str]) -> None:
    for name in (
        *REQUIRED_ENV,
        "MCP_ALLOWED_CLIENT_REDIRECTS",
        "SMARTMEMORY_WEB_URL",
        "MCP_HOSTED_PORT",
        "MCP_TRUST_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)


def test_from_env_reads_required_values(monkeypatch) -> None:
    _apply(monkeypatch, REQUIRED_ENV)

    cfg = HostedConfig.from_env()

    assert cfg.api_url == "https://api.test"
    assert cfg.public_base_url == "https://mcp.test"
    assert cfg.clerk_domain == "clerk.test"
    assert cfg.clerk_oauth_client_id == "client-id"
    assert cfg.clerk_oauth_client_secret == "client-secret"
    assert cfg.jwt_signing_key == "signing-key"
    assert cfg.state_encryption_key == "fernet-key"
    assert cfg.redis_url == "redis://localhost:6379/0"


def test_defaults_when_optional_vars_absent(monkeypatch) -> None:
    _apply(monkeypatch, REQUIRED_ENV)

    cfg = HostedConfig.from_env()

    assert cfg.hosted_port == 8012
    assert cfg.trust_proxy is False
    assert cfg.web_url == "https://app.smartmemory.ai"
    assert cfg.allowed_client_redirects == list(DEFAULT_ALLOWED_CLIENT_REDIRECTS)
    assert "https://claude.ai/api/mcp/auth_callback" in cfg.allowed_client_redirects
    assert "http://localhost:*" in cfg.allowed_client_redirects


def test_trailing_slash_stripped_from_urls(monkeypatch) -> None:
    _apply(
        monkeypatch,
        {
            **REQUIRED_ENV,
            "SMARTMEMORY_API_URL": "https://api.test/",
            "MCP_PUBLIC_BASE_URL": "https://mcp.test/",
        },
    )

    cfg = HostedConfig.from_env()

    assert cfg.api_url == "https://api.test"
    assert cfg.public_base_url == "https://mcp.test"


@pytest.mark.parametrize("missing", sorted(REQUIRED_ENV))
def test_missing_required_var_raises_naming_it(monkeypatch, missing: str) -> None:
    env = {k: v for k, v in REQUIRED_ENV.items() if k != missing}
    _apply(monkeypatch, env)

    with pytest.raises(RuntimeError) as excinfo:
        HostedConfig.from_env()

    assert missing in str(excinfo.value)


def test_blank_required_var_is_treated_as_missing(monkeypatch) -> None:
    _apply(monkeypatch, {**REQUIRED_ENV, "CLERK_DOMAIN": "   "})

    with pytest.raises(RuntimeError) as excinfo:
        HostedConfig.from_env()

    assert "CLERK_DOMAIN" in str(excinfo.value)


def test_optional_overrides(monkeypatch) -> None:
    _apply(
        monkeypatch,
        {
            **REQUIRED_ENV,
            "MCP_ALLOWED_CLIENT_REDIRECTS": "https://a.test/cb, https://b.test/cb ,",
            "SMARTMEMORY_WEB_URL": "https://web.test/",
            "MCP_HOSTED_PORT": "9999",
            "MCP_TRUST_PROXY": "true",
        },
    )

    cfg = HostedConfig.from_env()

    assert cfg.allowed_client_redirects == ["https://a.test/cb", "https://b.test/cb"]
    assert cfg.web_url == "https://web.test"
    assert cfg.hosted_port == 9999
    assert cfg.trust_proxy is True


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("true", True),
        ("TRUE", True),
        ("1", True),
        ("yes", True),
        ("false", False),
        ("0", False),
        ("", False),
    ],
)
def test_trust_proxy_parsing(monkeypatch, raw: str, expected: bool) -> None:
    _apply(monkeypatch, {**REQUIRED_ENV, "MCP_TRUST_PROXY": raw})

    assert HostedConfig.from_env().trust_proxy is expected


def test_non_numeric_port_raises_naming_the_var(monkeypatch) -> None:
    _apply(monkeypatch, {**REQUIRED_ENV, "MCP_HOSTED_PORT": "not-a-port"})

    with pytest.raises(RuntimeError) as excinfo:
        HostedConfig.from_env()

    assert "MCP_HOSTED_PORT" in str(excinfo.value)
