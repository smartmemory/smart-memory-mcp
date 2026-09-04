"""Mode selection and the `--http` loopback guard (PLAT-MCP-HOSTED-1 S6).

`--http` is single-identity: there is NO per-request authentication, so every
caller acts as whoever the process's API key belongs to. Binding it to 0.0.0.0
publishes one tenant's memory to the network. The guard forces loopback unless
the operator has said, in an environment variable, that they meant it.
"""

from __future__ import annotations

import logging

import pytest

from smartmemory_mcp import server

ALLOW = "SMARTMEMORY_MCP_ALLOW_UNAUTH_HTTP"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv(ALLOW, raising=False)
    monkeypatch.delenv("SMARTMEMORY_MCP_MODE", raising=False)


# --- the loopback guard ----------------------------------------------------------


def test_http_without_the_opt_in_is_forced_to_loopback(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        host, port = server.resolve_http_bind(["prog", "--http"])

    assert host == "127.0.0.1"
    assert port == 8011

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings, "a silently narrowed bind is exactly the degradation to avoid"
    message = warnings[0].getMessage()
    # The warning must name what was lost and how to get it back.
    assert "loopback only" in message
    assert ALLOW in message
    assert "0.0.0.0" in message
    assert "127.0.0.1" in message


def test_an_explicit_host_is_also_narrowed(caplog) -> None:
    with caplog.at_level(logging.WARNING):
        host, _ = server.resolve_http_bind(["prog", "--http", "--host", "192.168.1.5"])

    assert host == "127.0.0.1"
    assert any("192.168.1.5" in r.getMessage() for r in caplog.records)


def test_the_opt_in_keeps_the_requested_host(monkeypatch, caplog) -> None:
    monkeypatch.setenv(ALLOW, "true")

    with caplog.at_level(logging.WARNING):
        host, port = server.resolve_http_bind(
            ["prog", "--http", "--host", "0.0.0.0", "--port", "9099"]
        )

    assert host == "0.0.0.0"
    assert port == 9099
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_the_default_host_is_kept_under_the_opt_in(monkeypatch) -> None:
    monkeypatch.setenv(ALLOW, "true")

    assert server.resolve_http_bind(["prog", "--http"])[0] == "0.0.0.0"


@pytest.mark.parametrize("value", ["1", "true", "TRUE", "yes", "on"])
def test_truthy_opt_in_spellings(monkeypatch, value: str) -> None:
    monkeypatch.setenv(ALLOW, value)

    assert server.resolve_http_bind(["prog", "--http"])[0] == "0.0.0.0"


@pytest.mark.parametrize("value", ["", "0", "false", "no", "maybe"])
def test_anything_else_is_not_an_opt_in(monkeypatch, value: str) -> None:
    monkeypatch.setenv(ALLOW, value)

    assert server.resolve_http_bind(["prog", "--http"])[0] == "127.0.0.1"


@pytest.mark.parametrize("host", ["127.0.0.1", "localhost", "::1"])
def test_a_loopback_host_needs_no_opt_in_and_no_warning(caplog, host: str) -> None:
    with caplog.at_level(logging.WARNING):
        got, _ = server.resolve_http_bind(["prog", "--http", "--host", host])

    assert got == host
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_the_port_flag_still_works() -> None:
    assert server.resolve_http_bind(["prog", "--http", "--port", "8123"])[1] == 8123


# --- mode selection --------------------------------------------------------------


def test_hosted_is_off_by_default() -> None:
    assert server.hosted_mode_requested(["prog"]) is False
    assert server.hosted_mode_requested(["prog", "--http"]) is False


def test_the_hosted_flag_selects_hosted_mode() -> None:
    assert server.hosted_mode_requested(["prog", "--hosted"]) is True


@pytest.mark.parametrize("value", ["hosted", "HOSTED", " hosted "])
def test_the_env_var_selects_hosted_mode(monkeypatch, value: str) -> None:
    monkeypatch.setenv("SMARTMEMORY_MCP_MODE", value)

    assert server.hosted_mode_requested(["prog"]) is True


def test_another_mode_value_does_not_select_hosted(monkeypatch) -> None:
    monkeypatch.setenv("SMARTMEMORY_MCP_MODE", "stdio")

    assert server.hosted_mode_requested(["prog"]) is False


# --- main() dispatch -------------------------------------------------------------


def test_main_dispatches_to_run_hosted_with_a_config_from_env(monkeypatch) -> None:
    from ._hosted_fixtures import REQUIRED_HOSTED_ENV

    for name, value in REQUIRED_HOSTED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("SMARTMEMORY_MCP_MODE", "hosted")
    monkeypatch.setattr("sys.argv", ["smartmemory-mcp"])

    served: list = []
    monkeypatch.setattr(
        "smartmemory_mcp.hosted.server.run_hosted", lambda cfg: served.append(cfg)
    )
    # If main() fell through to the stdio branch this would block forever.
    monkeypatch.setattr(
        server.mcp, "run", lambda *a, **k: pytest.fail("stdio path must not run")
    )

    server.main()

    assert len(served) == 1
    assert served[0].api_url == REQUIRED_HOSTED_ENV["SMARTMEMORY_API_URL"]
    assert served[0].clerk_domain == REQUIRED_HOSTED_ENV["CLERK_DOMAIN"]
    assert served[0].hosted_port == 8012


def test_main_hosted_fails_loud_when_a_required_var_is_missing(monkeypatch) -> None:
    from ._hosted_fixtures import REQUIRED_HOSTED_ENV

    for name, value in REQUIRED_HOSTED_ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("MCP_JWT_SIGNING_KEY")
    monkeypatch.setattr("sys.argv", ["smartmemory-mcp", "--hosted"])
    monkeypatch.setattr(
        "smartmemory_mcp.hosted.server.run_hosted",
        lambda cfg: pytest.fail("must not serve with an incomplete config"),
    )

    with pytest.raises(RuntimeError) as excinfo:
        server.main()

    assert "MCP_JWT_SIGNING_KEY" in str(excinfo.value)


def test_main_default_path_serves_stdio(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["smartmemory-mcp"])
    calls: list = []
    monkeypatch.setattr(server.mcp, "run", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr(
        "smartmemory_mcp.hosted.server.run_hosted",
        lambda cfg: pytest.fail("stdio must not reach hosted mode"),
    )

    server.main()

    assert calls == [((), {"show_banner": False})]


def test_main_http_path_binds_loopback_by_default(monkeypatch) -> None:
    monkeypatch.setattr("sys.argv", ["smartmemory-mcp", "--http", "--port", "8099"])
    calls: list = []
    monkeypatch.setattr(server.mcp, "run", lambda *a, **k: calls.append(k))

    server.main()

    assert calls == [
        {
            "transport": "http",
            "host": "127.0.0.1",
            "port": 8099,
            "show_banner": False,
        }
    ]


# --- stdio registration is untouched ---------------------------------------------


def test_stdio_still_registers_its_tools_at_import() -> None:
    """The hosted branch must not have moved registration out from under stdio."""
    from ._tools import tool_names

    names = tool_names()

    # FREE tier is what an unconfigured environment resolves to; the point is
    # that registration still happened at import, not which tier it produced.
    assert "login" in names
    assert "whoami" in names
    assert "memory_search" in names
    assert len(names) >= 13
