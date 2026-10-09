"""An allowlisted KEY is not an allowlisted VALUE (MCP-MEMGET-META-1 fix round 1).

Table-driven over the class: every kept metadata value is type-checked, bounded,
single-lined and scrubbed, every drop logs a WARNING naming the key, and every
model-facing surface that serialises item metadata goes through the one helper.
"""

import json
import logging

import pytest

from smartmemory_mcp.hosted import effects
from smartmemory_mcp.tools import common, memory_tools
from smartmemory_mcp.tools.metadata_summary import (
    collect_item_metadata,
    compact_item,
    summarize_item_metadata,
)

SECRETS = (
    "TENANT_SECRET",
    "RAW_SECRET",
    "OWNER_SECRET",
    "SECURITY_SECRET",
    "FACT_SECRET",
    "KEY_SECRET",
    "PASS_SECRET",
    "PROFILE_SECRET",
)


def _summ(metadata, **item):
    return summarize_item_metadata({"item_id": "i1", "metadata": metadata, **item})


def _blob(value):
    return json.dumps(value)


NESTED = [
    ("tags", [{"tenant_id": "TENANT_SECRET", "existing_fact": "RAW_SECRET"}], "tags"),
    ("resolved_date", {"owner_key": "OWNER_SECRET"}, "resolved_dates"),
    ("resolved_dates", [{"date": {"owner_key": "OWNER_SECRET"}}], "resolved_dates"),
    ("created_at", {"owner_key": "OWNER_SECRET"}, "created_at"),
    ("source", {"tenant_id": "TENANT_SECRET"}, "source"),
    ("origin", ["RAW_SECRET"], "origin"),
    ("provenance", {"security_id": "SECURITY_SECRET"}, "provenance"),
    (
        "conflicts",
        [{"existing_item_id": {"security_id": "SECURITY_SECRET"}}],
        "conflicts.existing_item_id",
    ),
    (
        "conflicts",
        [{"existing_item_id": "o", "conflict_type": {"new_fact": "FACT_SECRET"}}],
        "conflicts.conflict_type",
    ),
    (
        "conflicts",
        [{"existing_item_id": "o", "explanation": {"new_fact": "FACT_SECRET"}}],
        "conflicts.explanation",
    ),
]


@pytest.mark.parametrize(("key", "value", "warned_key"), NESTED)
def test_nested_value_under_allowlisted_key_is_dropped_with_warning(
    key, value, warned_key, caplog
):
    with caplog.at_level(
        logging.WARNING, logger="smartmemory_mcp.tools.metadata_summary"
    ):
        lines = _summ({key: value})
        data = collect_item_metadata({"item_id": "i1", "metadata": {key: value}})
    text = "\n".join(lines) + _blob(data)
    for secret in SECRETS:
        assert secret not in text, (key, secret)
    assert "{" not in text.replace('{"', "").replace("{}", "") or key == "conflicts"
    assert any(warned_key in r.getMessage() for r in caplog.records), (
        key,
        [r.getMessage() for r in caplog.records],
    )


URL_ROWS = [
    (
        "https://u:PASS_SECRET@docs.test/tenants/TENANT_SECRET/note?api_key=KEY_SECRET#f",
        "https://docs.test/tenants/[redacted]/note",
    ),
    ("see https://docs.test/a?token=KEY_SECRET.", "see https://docs.test/a."),
    ("tenant_id=TENANT_SECRET", "tenant_id=[redacted]"),
    ("owner_key: OWNER_SECRET and more", "owner_key: [redacted] and more"),
    ("api_key=KEY_SECRET", "api_key=[redacted]"),
    ("Bearer abcdefghijklmnop", "Bearer [redacted]"),
    ("plain note from the user", "plain note from the user"),
]


@pytest.mark.parametrize("key", ["source", "origin", "provenance"])
@pytest.mark.parametrize(("raw", "expected"), URL_ROWS)
def test_source_values_are_scrubbed(key, raw, expected):
    data = collect_item_metadata({"item_id": "i1", "metadata": {key: raw}})
    assert data[key] == expected
    assert not any(s in data[key] for s in SECRETS)


def test_oversize_values_are_bounded_and_single_line():
    huge = "x" * 10_000
    meta = {
        "source": huge,
        "tags": [huge] * 50,
        "resolved_dates": [huge] * 50,
        "conflicts": [
            {
                "existing_item_id": "i\n" + huge,
                "conflict_type": "t\n" + huge,
                "explanation": "e\n" + huge,
            }
        ]
        * 50,
    }
    lines = _summ(meta)
    assert all("\n" not in line for line in lines)
    # worst case: 80 id + 60 type + 160 explanation + prefix and separators
    assert max(len(line) for line in lines) <= 330
    assert len([line for line in lines if line.startswith("conflicts with ")]) == 20
    assert lines[-1] == "(+30 more conflicts)"
    data = collect_item_metadata({"item_id": "i1", "metadata": meta})
    assert len(data["tags"]) == 20 and len(data["resolved_dates"]) == 10
    # worst case: 10 dates + 20 tags + 20 bounded conflicts
    assert len(_blob(data)) < 12_000


@pytest.mark.parametrize(
    "metadata",
    [
        {"challenge_result": {"conflicts": "garbage"}},
        {"challenge_result": "garbage"},
        {"challenge_result": {"conflicts": [1, "x", None]}},
        {"source": {"bad": 1}},
        {"tags": "garbage"},
        {"tags": [{"a": 1}, None]},
        {"conflicts": 7},
        {"resolved_dates": 5.5, "created_at": [1]},
    ],
)
def test_malformed_metadata_logs_warning_never_raises(metadata, caplog):
    with caplog.at_level(
        logging.WARNING, logger="smartmemory_mcp.tools.metadata_summary"
    ):
        lines = _summ(metadata)
    assert not any(line.startswith("conflicts with") for line in lines)
    assert any("dropped" in r.getMessage() for r in caplog.records), metadata


CONFLICT_ROWS = [
    (
        {
            "existing_item_id": "other\nsecond line",
            "conflict_type": "type\nthird",
            "explanation": "e",
        },
        "conflicts with other second line: type third: e",
    ),
    (
        {"existing_item_id": "i" * 500, "conflict_type": "t" * 500},
        "conflicts with " + "i" * 77 + "...: " + "t" * 57 + "...",
    ),
    (
        {"existing_item_id": 42, "conflict_type": "direct_contradiction"},
        "conflicts with 42: direct_contradiction",
    ),
    (
        {"conflict_type": None, "explanation": "only text"},
        "conflicts with ?: unknown: only text",
    ),
    (
        {"existing_item_id": "tenant_id=TENANT_SECRET", "conflict_type": "t"},
        "conflicts with tenant_id=[redacted]: t",
    ),
]


@pytest.mark.parametrize(("row", "expected"), CONFLICT_ROWS)
def test_conflict_fields_are_normalised(row, expected):
    assert _summ({"conflicts": [row]}) == [expected]


def test_single_conflict_dict_is_a_conflict_not_dropped():
    lines = _summ(
        {
            "conflicts": {
                "existing_item_id": "other-1",
                "conflict_type": "direct_contradiction",
                "explanation": "reason",
            }
        }
    )
    assert lines == ["conflicts with other-1: direct_contradiction: reason"]


# ---- every other surface that serialises item metadata ------------------- #

RICH = {
    "owner_key": "OWNER_SECRET",
    "activation": {"score": 0.5},
    "retrieval": {"profile": "PROFILE_SECRET"},
    "challenge_result": {
        "conflicts": [
            {
                "existing_item_id": "o1",
                "conflict_type": "contradiction",
                "existing_fact": "RAW_SECRET",
                "new_fact": "FACT_SECRET",
                "explanation": "differs",
            }
        ]
    },
    "tags": ["keep"],
    "effects_bundle": {"effects": [1]},
}


class _Registrar:
    def __init__(self):
        self.functions = {}

    def tool(self, **kwargs):
        def capture(function):
            self.functions[function.__name__] = function
            return function

        return capture


class _Backend:
    def __init__(self):
        self.item = {
            "item_id": "s1",
            "memory_type": "pending",
            "content": "note",
            "tenant_id": "TENANT_SECRET",
            "metadata": dict(RICH),
        }

    def get(self, *a, **k):
        return self.item

    def search(self, *a, **k):
        return [self.item]

    def supports(self, *a):
        return True

    def request(self, *a, **k):
        return {"items": [self.item], "total": 1}

    def read_around(self, *a, **k):
        return {"window_items": [self.item], "window_text": "note"}


@pytest.fixture
def registered(monkeypatch):
    registrar = _Registrar()
    memory_tools.register_free(registrar)
    effects.register(registrar)
    monkeypatch.setattr(common, "_backend", _Backend())
    return registrar.functions


SURFACES = [
    ("memory_search", {"query": "note", "cite": True}, "items"),
    ("get_working_context", {"session_id": "s", "query": "note"}, "items"),
    ("memory_recall", {"query": "note", "cite": True}, "items"),
    ("read_around", {"item_id": "s1"}, "window_items"),
    (
        "code_effects",
        {"repo": "r", "source_snapshot": "sha256:" + "a" * 64},
        "items",
    ),
]


@pytest.mark.parametrize(("name", "kwargs", "key"), SURFACES)
def test_every_metadata_surface_uses_the_allowlist(registered, name, kwargs, key):
    result = registered[name](**kwargs)
    item = result[key][0]
    blob = _blob(item)
    for secret in SECRETS:
        assert secret not in blob, (name, secret)
    # score_breakdown.activation is the contract's score, not metadata bookkeeping.
    meta_blob = _blob(item["metadata"])
    for bookkeeping in ("owner_key", "activation", "retrieval", "tenant_id"):
        assert bookkeeping not in meta_blob, (name, bookkeeping)
    assert "tenant_id" not in blob
    assert item["metadata"]["tags"] == ["keep"]
    assert item["metadata"]["conflicts"] == [
        {
            "existing_item_id": "o1",
            "conflict_type": "contradiction",
            "explanation": "differs",
        }
    ]
    assert item["content"] == "note"


def test_effects_keeps_envelope_and_bundle(registered):
    result = registered["code_effects"](repo="r", source_snapshot="sha256:" + "a" * 64)
    assert result["total"] == 1
    assert result["items"][0]["metadata"]["effects_bundle"] == {"effects": [1]}


def test_compact_item_passes_non_dicts_and_leaves_source_untouched():
    assert compact_item("x") == "x"
    src = {
        "item_id": "i",
        "tenant_id": "t",
        "metadata": {"owner_key": "o", "tags": ["a"]},
    }
    out = compact_item(src)
    assert out == {"item_id": "i", "metadata": {"tags": ["a"]}}
    assert src["metadata"]["owner_key"] == "o"
