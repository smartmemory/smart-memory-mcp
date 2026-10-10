"""Read tools show no metadata text by default (MCP-MEMGET-META-1, owner shape).

memory_get returns id, type and content, plus at most one conflict-count line.
Structured read tools return the item's own fields with ``metadata`` emptied
(``code_effects`` keeps only its requested ``effects_bundle``). Opt-in
``memory_get(include_metadata=True)`` is byte-identical to the 1353c49 output.
"""

import json
import logging
from pathlib import Path

import anyio
import pytest
from fastmcp import Client

from smartmemory_mcp.hosted import effects
from smartmemory_mcp.hosted import identity as hosted_identity
from smartmemory_mcp.hosted.exchange import ExchangeCache
from smartmemory_mcp.hosted.server import build_hosted_server
from smartmemory_mcp.tools import common, memory_tools

from ._hosted_fixtures import hosted_config, memory_store

FIXTURE = Path(__file__).parent / "fixtures" / "memget_a3_8f64f81f.json"
CANARY = "CANARY9853"
SNAPSHOT = "sha256:" + "a" * 64


def _memory_get_1353c49(item_id, item):
    """Golden copy of the 1353c49 memory_get body (the include_metadata=True path)."""
    if item is None:
        return f"Memory item not found: {item_id}"

    content = item["content"]
    mtype = item["memory_type"]
    meta = item["metadata"]

    parts = [
        f"Memory Item: {item_id}",
        f"Type: {mtype}",
        f"Content: {content}",
    ]
    if meta:
        parts.append(f"Metadata: {meta}")
    return "\n".join(parts)


class _Registrar:
    def __init__(self):
        self.functions = {}

    def tool(self, **kwargs):
        def capture(function):
            self.functions[function.__name__] = function
            return function

        return capture


class _Backend:
    item: dict

    def get(self, *args, **kwargs):
        return self.item

    def search(self, *args, **kwargs):
        return [self.item]

    def supports(self, *args):
        return True

    def request(self, *args, **kwargs):
        return {"items": [self.item], "total": 1}

    def read_around(self, *args, **kwargs):
        return {"window_items": [self.item], "window_text": "the note body"}


@pytest.fixture
def backend(monkeypatch):
    instance = _Backend()
    monkeypatch.setattr(common, "_backend", instance)
    return instance


@pytest.fixture
def tools(backend):
    registrar = _Registrar()
    memory_tools.register_free(registrar)
    effects.register(registrar)
    return registrar.functions


def _item(metadata, **top):
    return {
        "item_id": "item-1",
        "memory_type": "pending",
        "content": "the note body",
        "metadata": metadata,
        **top,
    }


# Every default output of the class, as text. memory_recall needs pending items.
CLASS_CALLS = {
    "memory_get": ("memory_get", {"item_id": "item-1"}),
    "memory_search cite": ("memory_search", {"query": "q", "cite": True}),
    "memory_search catalog": ("memory_search", {"query": "q", "catalog_mode": True}),
    "get_working_context": ("get_working_context", {"session_id": "s", "query": "q"}),
    "memory_recall cite": ("memory_recall", {"query": "q", "cite": True}),
    "read_around": ("read_around", {"item_id": "item-1"}),
    "code_effects": ("code_effects", {"repo": "r", "source_snapshot": SNAPSHOT}),
}


def _outputs(tools):
    out = {}
    for label, (name, kwargs) in CLASS_CALLS.items():
        result = tools[name](**kwargs)
        out[label] = result if isinstance(result, str) else json.dumps(result)
    return out


def _secret(field):
    return f"{field}:{CANARY}"


# Credential and free text in every metadata field the old summary used to show,
# plus raw top-level copies, properties and identity keys.
LEAK_FORMS = {
    "auth": "auth:" + CANARY,
    "sig": "sig:" + CANARY,
    "basic": "Basic YWxpY2U6" + CANARY,
    "url userinfo": f"https://u:{CANARY}@docs.test/n",
    "url query": f"https://docs.test/n?{CANARY}",
    "url fragment": f"see https://docs.test/n#{CANARY}",
    "api_key pair": f"api_key={CANARY}",
    "tenant path": f"https://h/tenants%2F{CANARY}/n",
    "json scalar": json.dumps({"tenant_id": CANARY}),
}


def _leaky_item(leak):
    conflict = {
        "existing_item_id": leak,
        "conflict_type": leak,
        "explanation": leak,
        "existing_fact": leak,
    }
    return _item(
        {
            "source": leak,
            "origin": leak,
            "provenance": leak,
            "title": leak,
            "tags": [leak],
            "created_at": leak,
            "resolved_dates": [{"date": leak}],
            "tenant_id": leak,
            "session_id": leak,
            "challenge_result": {
                "has_conflicts": True,
                "conflict_count": 1,
                "conflicts": [conflict],
            },
            "conflicts": [conflict],
        },
        origin=leak,
        source=leak,
        created_at=leak,
        valid_start_time=leak,
        properties={"note": leak},
        tenant_id=leak,
        mystery=leak,
    )


@pytest.mark.parametrize("leak", LEAK_FORMS.values(), ids=LEAK_FORMS.keys())
def test_no_metadata_text_in_any_default_output(tools, backend, leak):
    backend.item = _leaky_item(leak)
    for label, text in _outputs(tools).items():
        assert CANARY not in text, label
        assert "the note body" in text, label
    get = tools["memory_get"](item_id="item-1")
    assert get.splitlines()[-1] == (
        "This note conflicts with 1 other note. Use memory_explain for details, "
        "or memory_get(include_metadata=True)."
    )


@pytest.mark.parametrize("leak", LEAK_FORMS.values(), ids=LEAK_FORMS.keys())
def test_include_metadata_is_byte_identical_to_1353c49(tools, backend, leak):
    backend.item = _leaky_item(leak)
    out = tools["memory_get"](item_id="item-1", include_metadata=True)
    assert out == _memory_get_1353c49("item-1", backend.item)
    assert CANARY in out  # the opt-in is the full raw dump, unchanged


CLEAN = {
    "no metadata": ({}, None),
    "clean metadata, no conflicts": ({"tags": ["a"], "source": "doc"}, None),
    "challenge count": ({"challenge_result": {"conflict_count": 10}}, 10),
    "count wins over capped list": (
        {"challenge_result": {"conflict_count": 7, "conflicts": [{}, {}, {}]}},
        7,
    ),
    "challenge list only": ({"challenge_result": {"conflicts": [{}, {}]}}, 2),
    "bare conflicts list": ({"conflicts": [{"existing_item_id": "o"}]}, 1),
    "zero count": ({"challenge_result": {"conflict_count": 0}}, None),
    "empty list": ({"conflicts": []}, None),
    "challenge without conflicts": ({"challenge_result": {}}, None),
    "conflicts None": ({"conflicts": None}, None),
    "metadata None": (None, None),
    "single conflict dict": (
        {"conflicts": {"existing_item_id": "o", "conflict_type": "t"}},
        1,
    ),
    "wrapper dict": ({"conflicts": {"conflicts": [{}, {}]}}, 2),
    "id mapping": ({"conflicts": {"o1": {}, "o2": {}}}, 2),
    "empty wrapper": ({"conflicts": {"conflicts": {}}}, None),
    "empty dict": ({"conflicts": {}}, None),
}


@pytest.mark.parametrize(("metadata", "count"), CLEAN.values(), ids=CLEAN.keys())
def test_default_shape_and_count_line(tools, backend, metadata, count, caplog):
    backend.item = _item(metadata)
    with caplog.at_level(logging.WARNING, logger=memory_tools.logger.name):
        out = tools["memory_get"](item_id="item-1")
    lines = out.splitlines()
    assert lines[:3] == [
        "Memory Item: item-1",
        "Type: pending",
        "Content: the note body",
    ]
    if count is None:
        assert len(lines) == 3
    else:
        notes = "note" if count == 1 else "notes"
        assert lines[3:] == [
            f"This note conflicts with {count} other {notes}. Use memory_explain "
            "for details, or memory_get(include_metadata=True)."
        ]
    assert not [r for r in caplog.records if "conflict count" in r.getMessage()]
    assert tools["memory_get"](
        item_id="item-1", include_metadata=True
    ) == _memory_get_1353c49("item-1", backend.item)


MALFORMED = {
    "metadata str": ("garbage", "metadata is str"),
    "metadata list": ([1], "metadata is list"),
    "challenge str": ({"challenge_result": "garbage"}, "challenge_result is str"),
    "challenge list": ({"challenge_result": [1]}, "challenge_result is list"),
    "challenge int": ({"challenge_result": 17}, "challenge_result is int"),
    "huge int": ({"challenge_result": {"conflict_count": 10**400}}, "conflict_count"),
    "negative": ({"challenge_result": {"conflict_count": -1}}, "conflict_count"),
    "nan": ({"challenge_result": {"conflict_count": float("nan")}}, "conflict_count"),
    "inf": ({"challenge_result": {"conflict_count": float("inf")}}, "conflict_count"),
    "float": ({"challenge_result": {"conflict_count": 2.5}}, "conflict_count"),
    "bool": ({"challenge_result": {"conflict_count": True}}, "conflict_count"),
    "string count": ({"challenge_result": {"conflict_count": "10"}}, "conflict_count"),
    "None count": ({"challenge_result": {"conflict_count": None}}, "conflict_count"),
    "nested junk count": (
        {"challenge_result": {"conflict_count": {"x": [1, {"y": None}]}}},
        "conflict_count",
    ),
    "conflicts str": ({"conflicts": "garbage"}, "metadata.conflicts is str"),
    "conflicts int": ({"conflicts": 10**400}, "metadata.conflicts is int"),
    "conflicts dict": ({"conflicts": {"a": 1}}, "metadata.conflicts is a dict"),
    "wrapper junk": (
        {"conflicts": {"conflicts": "x"}},
        "metadata.conflicts.conflicts is str",
    ),
    "challenge conflicts str": (
        {"challenge_result": {"conflicts": "x"}},
        "challenge_result.conflicts is str",
    ),
    "nested junk entries": (
        {"conflicts": [None, "x", [1], {"ok": 1}]},
        "metadata.conflicts has entries",
    ),
}


@pytest.mark.parametrize(
    ("metadata", "named"), MALFORMED.values(), ids=MALFORMED.keys()
)
def test_malformed_count_warns_and_omits_the_line(
    tools, backend, metadata, named, caplog
):
    backend.item = _item(metadata)
    with caplog.at_level(logging.WARNING, logger=memory_tools.logger.name):
        out = tools["memory_get"](item_id="item-1")
    assert out.splitlines() == [
        "Memory Item: item-1",
        "Type: pending",
        "Content: the note body",
    ]
    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, warnings
    assert "item-1" in warnings[0] and named in warnings[0], warnings[0]
    # Every other class tool also returns normally for the same item. Catalog mode
    # reads metadata["stale"] and has raised on non-dict metadata since before
    # this change, so it is only checked for dict metadata.
    if not isinstance(metadata, dict):
        calls = dict(CLASS_CALLS)
        del calls["memory_search catalog"]
        for name, kwargs in calls.values():
            result = tools[name](**kwargs)
            assert "the note body" in (
                result if isinstance(result, str) else json.dumps(result)
            ), name
        return
    for label, text in _outputs(tools).items():
        assert "the note body" in text, label


@pytest.mark.parametrize("value", [10**400, float("nan"), "big", None, {"x": [1]}])
def test_odd_scores_never_raise_in_structured_tools(tools, backend, value):
    backend.item = _item({}, score_breakdown={"relevance": value}, confidence=value)
    for label in ("read_around", "code_effects", "get_working_context"):
        name, kwargs = CLASS_CALLS[label]
        assert tools[name](**kwargs)


SELECTORS = [
    ({"session_id": "s1"}, "s1", True),
    ({"conversation_id": "s1"}, "s1", True),
    ({"session_id": "s1"}, "other", False),
    ({}, "s1", False),
]


@pytest.mark.parametrize("cite", [True, False])
@pytest.mark.parametrize(("metadata", "selector", "match"), SELECTORS)
def test_recall_session_selectors_read_raw_metadata(
    tools, backend, metadata, selector, match, cite
):
    backend.item = _item(metadata)
    out = tools["memory_recall"](query="note", session_id=selector, cite=cite)
    if cite:
        assert [i["item_id"] for i in out["items"]] == (["item-1"] if match else [])
        assert all(i["metadata"] == {} for i in out["items"])
    else:
        assert ("the note body" in out) is match


@pytest.mark.parametrize("cite", [True, False])
@pytest.mark.parametrize("metadata", ["garbage", [1], 17])
def test_recall_malformed_metadata_is_skipped_with_warning(
    tools, backend, metadata, cite, caplog
):
    backend.item = _item(metadata)
    with caplog.at_level(logging.WARNING, logger=memory_tools.logger.name):
        out = tools["memory_recall"](query="note", session_id="s1", cite=cite)
    if cite:
        assert out["items"] == [] and out["citations"] == []
    else:
        assert out == "No prior turns found for this session."
    assert any(
        "item-1" in r.getMessage() and "not a dict" in r.getMessage()
        for r in caplog.records
    )


def test_structured_items_keep_their_own_fields(tools, backend):
    backend.item = _item(
        {"tags": ["x"]},
        score=0.5,
        confidence=0.9,
        stale=False,
        superseded=True,
        superseded_by="item-2",
        derived_from="item-0",
        reference=False,
        as_of_resolution="unresolved",
        origin="user",
        created_at="2026-10-09",
        properties={"a": 1},
    )
    item = tools["memory_search"](query="q", cite=True)["items"][0]
    assert item == {
        "item_id": "item-1",
        "memory_type": "pending",
        "content": "the note body",
        "metadata": {},
        "score": 0.5,
        "confidence": 0.9,
        "stale": False,
        "superseded": True,
        "superseded_by": "item-2",
        "derived_from": "item-0",
        "reference": False,
        "as_of_resolution": "unresolved",
    }


def test_effects_keeps_only_the_bundle(tools, backend):
    backend.item = _item({"effects_bundle": json.dumps({"atoms": []}), "repo": "r"})
    out = tools["code_effects"](repo="r", source_snapshot=SNAPSHOT)
    assert out["items"] == [
        {
            "item_id": "item-1",
            "memory_type": "pending",
            "content": "the note body",
            "metadata": {"effects_bundle": {"atoms": []}},
        }
    ]
    assert out["total"] == 1


def test_effects_corrupt_bundle_still_raises(tools, backend):
    backend.item = _item({"effects_bundle": "{not json"})
    with pytest.raises(ValueError, match="Corrupt uploaded effects bundle"):
        tools["code_effects"](repo="r", source_snapshot=SNAPSHOT)


@pytest.mark.parametrize("metadata", ["garbage", [1], 17])
def test_effects_malformed_metadata_warns(tools, backend, metadata, caplog):
    backend.item = _item(metadata)
    with caplog.at_level(logging.WARNING, logger=effects.log.name):
        out = tools["code_effects"](repo="r", source_snapshot=SNAPSHOT)
    assert out["items"][0]["metadata"] == {}
    assert any("item-1" in r.getMessage() for r in caplog.records)


def test_non_dict_items_are_dropped_with_warning(tools, backend, monkeypatch, caplog):
    backend.item = _item({})
    monkeypatch.setattr(
        backend, "read_around", lambda *a, **k: {"window_items": ["junk", backend.item]}
    )
    monkeypatch.setattr(backend, "request", lambda *a, **k: {"items": [7]})
    with caplog.at_level(logging.WARNING):
        around = tools["read_around"](item_id="item-1")
        fx = tools["code_effects"](repo="r", source_snapshot=SNAPSHOT)
    assert [i["item_id"] for i in around["window_items"]] == ["item-1"]
    assert fx["items"] == []
    messages = " ".join(r.getMessage() for r in caplog.records)
    assert "read_around dropped a str entry" in messages
    assert "code_effects dropped a int entry" in messages


def test_a3_real_note_default_is_content_plus_one_line(tools, backend):
    fixture = json.loads(FIXTURE.read_text())
    backend.item = fixture
    item_id = fixture["item_id"]
    old = tools["memory_get"](item_id=item_id, include_metadata=True)
    new = tools["memory_get"](item_id=item_id)
    assert old == _memory_get_1353c49(item_id, fixture)
    assert new == "\n".join(
        [
            f"Memory Item: {item_id}",
            f"Type: {fixture['memory_type']}",
            f"Content: {fixture['content']}",
            "This note conflicts with 10 other notes. Use memory_explain for "
            "details, or memory_get(include_metadata=True).",
        ]
    )
    assert len(new) < 0.25 * len(old), (len(old), len(new))


@pytest.fixture
def _restore_hosted_mode():
    before = hosted_identity.hosted_mode_enabled()
    try:
        yield
    finally:
        hosted_identity.set_hosted_mode(before)


def test_hosted_and_local_servers_expose_include_metadata(_restore_hosted_mode):
    server = build_hosted_server(
        hosted_config(), redis_store=memory_store(), exchange_cache=ExchangeCache()
    )

    async def hosted_tool():
        async with Client(server) as client:
            return next(t for t in await client.list_tools() if t.name == "memory_get")

    hosted = anyio.run(hosted_tool)
    hosted_identity.set_hosted_mode(False)
    from smartmemory_mcp.server import mcp as local_server

    async def local_tool():
        return await local_server.get_tool("memory_get")

    local = anyio.run(local_tool)
    for label, tool in {"hosted": hosted, "local": local}.items():
        schema = getattr(tool, "input_schema", None) or tool.parameters
        props = schema["properties"]
        assert props["include_metadata"]["type"] == "boolean", label
        assert props["include_metadata"]["default"] is False, label
        assert schema["required"] == ["item_id"], label
        assert "include_metadata=True" in tool.description, label
        assert "memory_explain" in tool.description, label
