"""OUTREACH-MARIUS-HITL-SURFACE-1 connector wording on the HOSTED connector, against the real service app.

Same harness as ``test_hosted_decisions_connector_notes.py`` (its fixtures are reused, loaded by path), the notes
of ``_connector_wording_notes.py``. Run it the same way: copied with both helper files and the sibling test into a
service checkout's ``tests/security/``, ``DECISIONS_CONNECTOR_API_PORT`` set to a reserved test port. In this repo's
own run it skips (no ``service``).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytest.importorskip("service")
pytest.importorskip("service_common.testing")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.usefixtures("cleanup_module_falkordb_graphs"),
]


def _load(name: str):
    spec = importlib.util.spec_from_file_location(
        name, Path(__file__).with_name(f"{name}.py")
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_host = _load("test_hosted_decisions_connector_notes")
notes = _load("_connector_wording_notes")
api_url = _host.api_url
hosted_tools = _host.hosted_tools
_propose_workspaces = _host._propose_workspaces
arms = _host.arms


@pytest.mark.parametrize("note", notes.NOTES, ids=lambda n: n.__name__)
def test_note_on_the_hosted_connector(arms, note):
    w1, _, _ = arms
    note(w1)
