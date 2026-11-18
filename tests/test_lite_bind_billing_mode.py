"""MCP-LITE-BIND-BILLING-MODE-1: the Lite entry binds billing.mode=local, the hosted entry does not."""

import logging
import sys
import types

import pytest

from smartmemory_mcp import server

ENV = "SMARTMEMORY_BILLING_MODE"
WARN = "core without smartmemory.billing; billing mode not bound for Lite"

# (id, hosted, core has billing module, env before, env after, bind called, WARNING logged)
ROWS = [
    ("local_unset", False, True, None, "local", True, False),
    ("local_explicit_enforce", False, True, "enforce", "enforce", True, False),
    ("hosted_unset", True, True, None, None, False, False),
    ("old_core_unset", False, False, None, None, False, True),
]


@pytest.mark.parametrize(
    "hosted,has_core,before,after,bound,warned",
    [r[1:] for r in ROWS],
    ids=[r[0] for r in ROWS],
)
def test_main_binds_billing_mode(
    monkeypatch, caplog, hosted, has_core, before, after, bound, warned
):
    calls = []

    if has_core:
        import os

        def bind(environ=None):
            calls.append(1)
            os.environ.setdefault(ENV, "local")

        mode = types.ModuleType("smartmemory.billing.mode")
        mode.bind_local_mode_for_lite = bind
        billing = types.ModuleType("smartmemory.billing")
        billing.mode = mode
        pkg = types.ModuleType("smartmemory")
        pkg.billing = billing
        monkeypatch.setitem(sys.modules, "smartmemory", pkg)
        monkeypatch.setitem(sys.modules, "smartmemory.billing", billing)
        monkeypatch.setitem(sys.modules, "smartmemory.billing.mode", mode)
    else:
        # None in sys.modules makes the import raise ImportError, as an older core would.
        monkeypatch.setitem(sys.modules, "smartmemory.billing.mode", None)

    if before is None:
        monkeypatch.delenv(ENV, raising=False)
    else:
        monkeypatch.setenv(ENV, before)

    monkeypatch.setattr(server, "hosted_mode_requested", lambda argv=None: hosted)
    monkeypatch.setattr(server, "_TRANSCRIPT_TOOLS_REGISTERED", False)
    monkeypatch.setattr(server.mcp, "run", lambda **kw: None)
    monkeypatch.setattr(sys, "argv", ["smartmemory-mcp"])
    ran_hosted = []
    hosted_pkg = types.ModuleType("smartmemory_mcp.hosted.server")
    hosted_pkg.run_hosted = lambda cfg: ran_hosted.append(cfg)
    cfg_pkg = types.ModuleType("smartmemory_mcp.hosted.config")
    cfg_pkg.HostedConfig = types.SimpleNamespace(from_env=lambda: object())
    monkeypatch.setitem(sys.modules, "smartmemory_mcp.hosted.server", hosted_pkg)
    monkeypatch.setitem(sys.modules, "smartmemory_mcp.hosted.config", cfg_pkg)

    with caplog.at_level(logging.WARNING, logger=server.logger.name):
        server.main()

    import os

    assert os.environ.get(ENV) == after
    assert bool(calls) is bound
    assert bool(ran_hosted) is hosted
    assert any(WARN in r.getMessage() for r in caplog.records) is warned
