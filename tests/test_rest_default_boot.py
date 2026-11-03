"""MCP-PERF-1: in the default REST mode, boot and the memory tools load no models.

Arms of the two in-process paths the MCP still has:
- the boot warm-up of the transcript search models (local opt-in only);
- the transcript store itself, which is local-only and says so at WARNING when it is
  opened from a REST-mode process.
"""

from __future__ import annotations

import logging
import sys
import types

import pytest

from smartmemory_mcp.backends import dispatch


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv("SMARTMEMORY_MCP_MODE", "remote")  # tracked, restored after
    monkeypatch.delenv("SMARTMEMORY_MCP_MODE")
    dispatch.reset_backend()
    yield
    dispatch.reset_backend()


# (row id, SMARTMEMORY_MCP_MODE, transcript tools registered, warm-up expected, probe expected)
BOOT_ROWS = [
    ("unset+pro", None, True, False, True),
    ("remote+pro", "remote", True, False, True),
    ("local+pro", "local", True, True, False),
    ("local+free", "local", False, False, False),
    ("garbage+pro", "bogus", True, False, True),
]


@pytest.mark.parametrize(
    "mode,registered,warm,probe",
    [r[1:] for r in BOOT_ROWS],
    ids=[r[0] for r in BOOT_ROWS],
)
def test_boot_warm_up_only_on_local_opt_in(monkeypatch, mode, registered, warm, probe):
    import smartmemory_mcp.server as server
    from smartmemory_mcp.tools import transcript_tools

    if mode is not None:
        monkeypatch.setenv("SMARTMEMORY_MCP_MODE", mode)
    calls: list[str] = []
    monkeypatch.setattr(server, "_TRANSCRIPT_TOOLS_REGISTERED", registered)
    monkeypatch.setattr(
        transcript_tools, "schedule_warm_start", lambda: calls.append("warm")
    )
    monkeypatch.setattr(
        dispatch,
        "check_service_reachable",
        lambda url, timeout=2.0: calls.append("probe"),
    )
    monkeypatch.setattr(server.mcp, "run", lambda **kw: calls.append("run"))
    monkeypatch.setattr(sys, "argv", ["smartmemory-mcp"])

    server.main()
    for thread in __import__("threading").enumerate():
        if thread.name == "sm-api-probe":
            thread.join(5)

    assert ("warm" in calls) is warm
    assert ("probe" in calls) is probe
    assert calls[-1] == "run" or calls[-1] == "probe"


@pytest.mark.parametrize(
    "mode,warns", [(None, True), ("remote", True), ("local", False)]
)
def test_transcript_store_open_warns_outside_local(
    monkeypatch, caplog, tmp_path, mode, warns
):
    from smartmemory_mcp.tools import transcript_tools

    if mode is not None:
        monkeypatch.setenv("SMARTMEMORY_MCP_MODE", mode)
    monkeypatch.setenv("SMARTMEMORY_TRANSCRIPTS_DIR", str(tmp_path))
    factory = types.ModuleType("smartmemory.tools.factory")
    factory.create_lite_memory = lambda data_dir, pipeline_profile=None: object()
    monkeypatch.setitem(sys.modules, "smartmemory.tools.factory", factory)
    transcript_tools.reset_memory()
    try:
        with caplog.at_level(logging.WARNING, logger=transcript_tools.logger.name):
            transcript_tools._get_memory()
    finally:
        transcript_tools.reset_memory()
    hits = [r for r in caplog.records if "in-process" in r.getMessage()]
    assert bool(hits) is warns
    if warns:
        assert str(tmp_path) in hits[0].getMessage()
