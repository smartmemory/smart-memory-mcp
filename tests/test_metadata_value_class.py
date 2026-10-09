"""Model-facing item output is built by construction (MCP-MEMGET-META-1 final fix).

Table-driven over the class: only allowlisted fields are emitted, every kept
value is validated by type (ISO dates, rebuilt URLs, bounded labels/tags/ids,
marker-free free text), every drop and truncation logs a WARNING naming the key
(never the value), compaction runs once per tool and is idempotent, and every
model-facing surface that serialises items goes through the one helper.
"""

import json
import logging

import pytest

from smartmemory_mcp.backends.models import normalize_item
from smartmemory_mcp.hosted import effects
from smartmemory_mcp.tools import common, memory_tools
from smartmemory_mcp.tools.metadata_summary import (
    collect_item_metadata,
    compact_item,
    compact_items,
    summarize_item_metadata,
)

LOGGER = "smartmemory_mcp.tools.metadata_summary"

SECRETS = (
    "TENANT_SECRET",
    "RAW_SECRET",
    "OWNER_SECRET",
    "SECURITY_SECRET",
    "FACT_SECRET",
    "KEY_SECRET",
    "PASS_SECRET",
    "PROFILE_SECRET",
    "P_SECRET",
    "K_SECRET",
    "F_SECRET",
    "T_SECRET",
    "FRAG_SECRET",
)


def _summ(metadata, **item):
    return summarize_item_metadata({"item_id": "i1", "metadata": metadata, **item})


def _blob(value):
    return json.dumps(value, default=str)


def _assert_clean(*outputs):
    text = "\n".join(o if isinstance(o, str) else _blob(o) for o in outputs)
    for secret in SECRETS:
        assert secret not in text, secret


def _messages(caplog):
    return [r.getMessage() for r in caplog.records if r.name == LOGGER]


# ---- R2-1: the reviewer's 5 leak strings x all 15 placements ------------- #

LEAKS = (
    "//u:P_SECRET@h/p?k=K_SECRET#F_SECRET",
    "mailto:u:P_SECRET@h?k=K_SECRET#F_SECRET",
    "https:////u:P_SECRET@h/p?k=K_SECRET#F_SECRET",
    "https://h/tenants%2FT_SECRET/n",
    '{"tenant_id":"T_SECRET","api_key":"K_SECRET"}',
)

PLACEMENTS = {
    "created_at": lambda v: {"created_at": v},
    "valid_start_time": lambda v: {"valid_start_time": v},
    "valid_end_time": lambda v: {"valid_end_time": v},
    "resolved_date": lambda v: {"resolved_date": v},
    "resolved_dates[]": lambda v: {"resolved_dates": [v]},
    "source": lambda v: {"source": v},
    "origin": lambda v: {"origin": v},
    "provenance": lambda v: {"provenance": v},
    "tags[]": lambda v: {"tags": [v]},
    "conflicts.existing_item_id": lambda v: {
        "conflicts": [{"existing_item_id": v, "conflict_type": "t"}]
    },
    "conflicts.conflicting_item_id": lambda v: {
        "conflicts": [{"conflicting_item_id": v, "conflict_type": "t"}]
    },
    "conflicts.item_id": lambda v: {
        "conflicts": [{"item_id": v, "conflict_type": "t"}]
    },
    "conflicts.id": lambda v: {"conflicts": [{"id": v, "conflict_type": "t"}]},
    "conflicts.conflict_type": lambda v: {
        "conflicts": [{"existing_item_id": "o", "conflict_type": v}]
    },
    "conflicts.explanation": lambda v: {
        "conflicts": [{"existing_item_id": "o", "conflict_type": "t", "explanation": v}]
    },
}


@pytest.mark.parametrize("placement", sorted(PLACEMENTS))
@pytest.mark.parametrize("leak", LEAKS)
def test_leak_strings_never_reach_output_in_any_placement(placement, leak, caplog):
    metadata = PLACEMENTS[placement](leak)
    item = {"item_id": "i1", "content": "c", "memory_type": "semantic"}
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        lines = summarize_item_metadata({**item, "metadata": metadata})
        compact = compact_item({**item, "metadata": metadata})
    _assert_clean(lines, compact)
    messages = _messages(caplog)
    assert any("dropped" in m for m in messages), (placement, messages)
    _assert_clean(*messages)  # warnings name the key, never the value


TOP_LEVEL_COPIES = (
    "created_at",
    "valid_start_time",
    "valid_end_time",
    "origin",
    "source",
)


@pytest.mark.parametrize("key", TOP_LEVEL_COPIES)
@pytest.mark.parametrize(
    "value",
    (
        *LEAKS,
        "api_key=KEY_SECRET",
        "tenant_id=TENANT_SECRET",
        "https://u:PASS_SECRET@h/p",
    ),
)
def test_top_level_copies_are_folded_never_copied_raw(key, value, caplog):
    raw = {"item_id": "i1", "content": "c", "memory_type": "semantic", key: value}
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        out = compact_item(raw)
    assert key not in out
    _assert_clean(out, *_messages(caplog))


def test_normalized_item_top_level_copies_are_validated():
    """R2-2 exact shape: a genuine normalize_item with secrets in origin/created_at."""
    item = normalize_item(
        {
            "item_id": "review-2",
            "content": "note",
            "memory_type": "pending",
            "metadata": {},
            "origin": "api_key=KEY_SECRET",
            "created_at": "tenant_id=TENANT_SECRET",
        }
    )
    out = compact_item(item)
    _assert_clean(out)
    assert set(out) <= {
        "item_id",
        "content",
        "memory_type",
        "metadata",
        "score",
        "confidence",
        "stale",
        "superseded",
        "superseded_by",
        "derived_from",
        "reference",
        "as_of_resolution",
    }


def test_raw_properties_unknown_and_identity_keys_are_dropped_and_named(caplog):
    raw = {
        "item_id": "i1",
        "content": "c",
        "memory_type": "semantic",
        "properties": {"tenant_id": "TENANT_SECRET"},
        "owner_key": "OWNER_SECRET",
        "security_id": "SECURITY_SECRET",
        "tenant_id": "TENANT_SECRET",
        "entities": [{"name": "RAW_SECRET"}],
        "drift_warnings": [{"x": "RAW_SECRET"}],
        "brand_new_field": "RAW_SECRET",
        "metadata": {"owner_key": "OWNER_SECRET", "tags": ["a"]},
    }
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        out = compact_item(raw)
    assert out == {
        "item_id": "i1",
        "content": "c",
        "memory_type": "semantic",
        "metadata": {"tags": ["a"]},
    }
    omitted = " ".join(m for m in _messages(caplog) if "omitted" in m)
    for key in (
        "properties",
        "owner_key",
        "security_id",
        "tenant_id",
        "entities",
        "drift_warnings",
        "brand_new_field",
        "metadata.owner_key",
    ):
        assert key in omitted, key
    _assert_clean(*_messages(caplog))


def test_top_level_values_are_type_validated(caplog):
    raw = {
        "item_id": "bad id with spaces",
        "memory_type": {"x": 1},
        "content": ["not text"],
        "score": float("nan"),
        "confidence": "high",
        "stale": "no",
        "superseded_by": "tenant_id=TENANT_SECRET",
        "as_of_resolution": "maybe",
        "score_breakdown": {"relevance": 0.5, "activation": "x", "unknown_part": 1},
    }
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        out = compact_item(raw)
    assert out == {"score_breakdown": {"relevance": 0.5}, "metadata": {}}
    messages = " ".join(_messages(caplog))
    for key in (
        "item_id",
        "memory_type",
        "content",
        "score",
        "confidence",
        "stale",
        "superseded_by",
        "as_of_resolution",
        "score_breakdown.activation",
        "score_breakdown.unknown_part",
    ):
        assert key in messages, key
    _assert_clean(out, messages)


# ---- value grammar ------------------------------------------------------- #

URL_KEPT = [
    ("https://docs.test/a/b", "https://docs.test/a/b", False),
    ("HTTPS://Docs.Test:8443/a", "https://docs.test:8443/a", False),
    (
        "https://u:PASS_SECRET@docs.test/note?api_key=KEY_SECRET#f",
        "https://docs.test/note",
        True,
    ),
    ("https://[::1]:443/a?code=KEY_SECRET#FRAG_SECRET", "https://[::1]:443/a", True),
    ("http://[2001:db8::1]/x", "http://[2001:db8::1]/x", False),
    ("ftp://10.0.0.1/pub/file.txt", "ftp://10.0.0.1/pub/file.txt", False),
    (
        "git+ssh://git@github.com/org/repo.git",
        "git+ssh://github.com/org/repo.git",
        True,
    ),
    ("https://docs.test/a%20b", "https://docs.test/a%20b", False),
]


@pytest.mark.parametrize("key", ["source", "origin", "provenance"])
@pytest.mark.parametrize(("raw", "expected", "altered"), URL_KEPT)
def test_allowed_urls_are_rebuilt(key, raw, expected, altered, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        data = collect_item_metadata({"item_id": "i1", "metadata": {key: raw}})
    assert data[key] == expected
    shortened = [m for m in _messages(caplog) if "shortened" in m]
    assert bool(shortened) == altered
    if altered:
        assert f"metadata.{key}" in shortened[0]
    _assert_clean(data, *_messages(caplog))


URL_DROPPED = [
    "//u:P_SECRET@h/p",  # scheme-relative
    "mailto:alice",  # opaque scheme
    "data:text/plain,x",
    "javascript:alert(1)",
    "urn:isbn:123",
    "file:///etc/passwd",
    "ws://h/x",  # scheme outside the allowlist
    "https:////h/p",  # extra slashes
    "https:///h/p",
    "https://h/tenants%2FT_SECRET/n",  # percent-encoded separator
    "https://h/a%252Fb",  # double-encoded
    "https://h/users/alice/x",  # identity path segment
    "https://h/a;jsessionid=X",  # path params
    "https://h/a@b",
    "https://h:99999/a",  # bad port
    "https://h_bad/a",  # bad host
    "https://h/a b",  # whitespace
    "https://h/" + "a" * 300,  # too long
    "/Users/alice/notes.md",  # home path label
    "tenant_id=TENANT_SECRET",
    "owner_key: OWNER_SECRET",
    "api_key=KEY_SECRET",
    "Bearer abcdefghijklmnop",
    "tenant:TENANT_SECRET",
    "x" * 81,
]


@pytest.mark.parametrize("key", ["source", "origin", "provenance"])
@pytest.mark.parametrize("raw", URL_DROPPED)
def test_other_source_forms_are_dropped_with_warning(key, raw, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        data = collect_item_metadata({"item_id": "i1", "metadata": {key: raw}})
    assert key not in data
    assert any(f"metadata.{key}" in m and "dropped" in m for m in _messages(caplog))
    _assert_clean(*_messages(caplog))


@pytest.mark.parametrize(
    "label",
    [
        "import:vault",
        "user:zettel",
        "evolver:decision",
        "mcp",
        "notes/design.md",
        "manual",
    ],
)
def test_plain_labels_are_kept(label):
    assert collect_item_metadata({"metadata": {"origin": label}}) == {"origin": label}


EXPLANATIONS_DROPPED = [
    "see tenant%5Fid%3DT_SECRET",  # percent-encoded marker
    '{"owner_key": "OWNER_SECRET"}',  # JSON
    '"\\u0074enant_id T_SECRET"',  # JSON unicode escape
    "tenant&#95;id T_SECRET",  # HTML entity
    "contact alice@example.com",  # identity
    "uses token KEY_SECRET",
    "at https://h/p?k=K_SECRET",
    "key eyJhbGciOiJIUzI1NiJ9.payload",
]


@pytest.mark.parametrize("text", EXPLANATIONS_DROPPED)
def test_explanations_with_markers_are_dropped_whole(text, caplog):
    row = {"existing_item_id": "o", "conflict_type": "t", "explanation": text}
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        lines = _summ({"conflicts": [row]})
    assert lines == ["conflicts with o: t"]
    assert any("conflicts[0].explanation" in m for m in _messages(caplog))
    _assert_clean(lines, *_messages(caplog))


def test_long_explanation_is_truncated_with_warning(caplog):
    row = {"existing_item_id": "o", "conflict_type": "t", "explanation": "word " * 100}
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        lines = _summ({"conflicts": [row]})
    assert len(lines) == 1 and lines[0].endswith("...")
    assert len(lines[0]) <= len("conflicts with o: t: ") + 160
    assert any("shortened" in m and "explanation" in m for m in _messages(caplog))


def test_oversize_explanation_is_dropped_before_decoding(caplog):
    row = {"existing_item_id": "o", "conflict_type": "t", "explanation": "x" * 25_000}
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        lines = _summ({"conflicts": [row]})
    assert lines == ["conflicts with o: t"]
    assert any(
        "explanation" in m and "longer than 20000" in m for m in _messages(caplog)
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-10-09", "2026-10-09"),
        ("2026-10-09T05:53:07.452072+00:00", "2026-10-09T05:53:07.452072+00:00"),
        ("2026-10-09T05:53:07Z", "2026-10-09T05:53:07+00:00"),
        ("next tuesday", None),
        ("2026-13-40", None),
        (1_700_000_000, None),
    ],
)
def test_dates_must_parse_as_iso(raw, expected):
    data = collect_item_metadata({"metadata": {"created_at": raw}})
    assert data.get("created_at") == expected


# ---- R2-4: every drop and truncation warns, including falsy values ------- #

BAD_VALUES = {
    "False": False,
    "zero-length bytes": b"",
    "bytes": b"RAW_SECRET",
    "empty dict": {},
    "empty list in scalar slot": [],
    "nested dict": {"owner_key": "OWNER_SECRET"},
    "nan": float("nan"),
}

SCALAR_PLACEMENTS = [p for p in sorted(PLACEMENTS) if p != "tags[]"] + ["tags[]"]


@pytest.mark.parametrize("placement", SCALAR_PLACEMENTS)
@pytest.mark.parametrize("label", sorted(BAD_VALUES))
def test_falsy_compound_and_bytes_values_warn(placement, label, caplog):
    value = BAD_VALUES[label]
    if label == "empty list in scalar slot" and placement in (
        "resolved_date",
        "resolved_dates[]",
    ):
        pytest.skip("an empty resolved-date list is absence, not a value")
    metadata = PLACEMENTS[placement](value)
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        data = collect_item_metadata({"item_id": "i1", "metadata": metadata})
    _assert_clean(data, *_messages(caplog))
    assert any("dropped" in m for m in _messages(caplog)), (placement, label)


@pytest.mark.parametrize(
    "metadata",
    [
        {"source": False, "origin": {}, "provenance": [], "created_at": b""},
        {"resolved_dates": [{"owner_key": "OWNER_SECRET"}]},
        {"resolved_dates": [{"date": None, "text": "someday"}]},
        {
            "conflicts": [
                {"existing_item_id": "o", "conflict_type": {}, "explanation": False}
            ]
        },
        {"source": "x" * 1000},
        {"challenge_result": "garbage"},
        {"challenge_result": {"conflicts": [1, "x"]}},
        {"conflicts": 7},
        {"tags": "garbage"},
        {"conflicts_more": "many"},
    ],
)
def test_reviewer_silent_drop_rows_now_warn(metadata, caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        lines = _summ(metadata)
    assert any("dropped" in m for m in _messages(caplog)), metadata
    _assert_clean(lines, *_messages(caplog))


def test_none_and_blank_are_absence_not_drops(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        data = collect_item_metadata(
            {
                "item_id": "i1",
                "created_at": "",
                "valid_start_time": None,
                "metadata": {"source": None, "tags": [], "origin": "  "},
            }
        )
    assert data == {}
    assert _messages(caplog) == []


def test_caps_warn_and_lines_stay_bounded(caplog):
    huge = "x" * 10_000
    meta = {
        "tags": [f"t{i}" for i in range(50)],
        "resolved_dates": [f"2026-01-{d:02d}" for d in range(1, 29)],
        "conflicts": [
            {
                "existing_item_id": f"o{i}",
                "conflict_type": "t",
                "explanation": "e\n" + huge,
            }
            for i in range(50)
        ],
    }
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        lines = _summ(meta)
        data = collect_item_metadata({"item_id": "i1", "metadata": meta})
    assert all("\n" not in line for line in lines)
    assert max(len(line) for line in lines) <= 400
    assert len([line for line in lines if line.startswith("conflicts with ")]) == 20
    assert lines[-1] == "(+30 more conflicts)"
    assert len(data["tags"]) == 20 and len(data["resolved_dates"]) == 10
    messages = " ".join(_messages(caplog))
    for needle in (
        "metadata.tags",
        "resolved_dates",
        "capped at 20 of 50",
        "shortened",
    ):
        assert needle in messages, needle


CONFLICT_ROWS = [
    (
        {
            "existing_item_id": "other\nsecond",
            "conflict_type": "type\nthird",
            "explanation": "e",
        },
        ["conflicts with ?: type third: e"],
    ),
    ({"existing_item_id": "i" * 500, "conflict_type": "t" * 500}, []),
    (
        {"existing_item_id": 42, "conflict_type": "direct_contradiction"},
        ["conflicts with 42: direct_contradiction"],
    ),
    (
        {"conflict_type": None, "explanation": "only text"},
        ["conflicts with ?: unknown: only text"],
    ),
    (
        {"existing_item_id": "tenant_id=TENANT_SECRET", "conflict_type": "t"},
        ["conflicts with ?: t"],
    ),
]


@pytest.mark.parametrize(("row", "expected"), CONFLICT_ROWS)
def test_conflict_fields_are_validated(row, expected):
    assert _summ({"conflicts": [row]}) == expected


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


# ---- R2-7: compaction is idempotent ------------------------------------- #


def test_double_compaction_is_idempotent_and_keeps_overflow():
    item = {
        "item_id": "i1",
        "content": "c",
        "memory_type": "semantic",
        "created_at": "2026-10-09T05:53:07+00:00",
        "origin": "import:vault",
        "metadata": {
            "source": "https://u:p@docs.test/a?x=1",
            "tags": ["a", "b"],
            "resolved_dates": [{"date": "2026-09-30", "text": "2026-09-30"}],
            "conflicts": [
                {
                    "existing_item_id": f"o{i}",
                    "conflict_type": "t",
                    "explanation": "e" * 300,
                }
                for i in range(50)
            ],
        },
    }
    once = compact_item(item)
    twice = compact_item(once)
    assert once == twice
    assert once["metadata"]["conflicts_more"] == 30
    assert len(once["metadata"]["conflicts"]) == 20


def test_compact_items_drops_non_dicts_with_warning(caplog):
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        out = compact_items(["RAW_SECRET", None, {"item_id": "i"}])
    assert out == [{"item_id": "i", "metadata": {}}]
    assert compact_item("x") is None
    # two from compact_items, one from compact_item("x")
    assert len([m for m in _messages(caplog) if "dropped an item" in m]) == 3


def test_compact_item_leaves_source_untouched():
    src = {
        "item_id": "i",
        "tenant_id": "t",
        "metadata": {"owner_key": "o", "tags": ["a"]},
    }
    assert compact_item(src) == {"item_id": "i", "metadata": {"tags": ["a"]}}
    assert src["metadata"]["owner_key"] == "o" and src["tenant_id"] == "t"


# ---- every surface that serialises items -------------------------------- #

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
            "origin": "api_key=KEY_SECRET",
            "created_at": "tenant_id=TENANT_SECRET",
            "properties": {"tenant_id": "TENANT_SECRET"},
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
def backend(monkeypatch):
    instance = _Backend()
    monkeypatch.setattr(common, "_backend", instance)
    return instance


@pytest.fixture
def registered(backend):
    registrar = _Registrar()
    memory_tools.register_free(registrar)
    effects.register(registrar)
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
def test_every_item_surface_uses_the_allowlist(registered, name, kwargs, key):
    result = registered[name](**kwargs)
    item = result[key][0]
    _assert_clean(item)
    meta_blob = _blob(item["metadata"])
    for bookkeeping in ("owner_key", "activation", "retrieval", "tenant_id"):
        assert bookkeeping not in meta_blob, (name, bookkeeping)
    for raw_key in ("tenant_id", "origin", "created_at", "properties"):
        assert raw_key not in item, (name, raw_key)
    assert item["metadata"]["tags"] == ["keep"]
    assert item["metadata"]["conflicts"] == [
        {
            "existing_item_id": "o1",
            "conflict_type": "contradiction",
            "explanation": "differs",
        }
    ]
    assert item["content"] == "note"


def test_memory_get_default_is_clean(registered):
    out = registered["memory_get"](item_id="s1")
    _assert_clean(out)
    assert "conflicts with o1: contradiction: differs" in out


def test_effects_keeps_envelope_and_bundle(registered):
    result = registered["code_effects"](repo="r", source_snapshot="sha256:" + "a" * 64)
    assert result["total"] == 1
    assert result["items"][0]["metadata"]["effects_bundle"] == {"effects": [1]}


@pytest.mark.parametrize("metadata", ["garbage", [1], 7])
def test_effects_malformed_metadata_degrades_with_warning(
    registered, backend, metadata, caplog
):
    backend.item = {
        "item_id": "s1",
        "content": "c",
        "memory_type": "fa_snapshot",
        "metadata": metadata,
    }
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = registered["code_effects"](
            repo="r", source_snapshot="sha256:" + "a" * 64
        )
    assert result["items"] == [
        {"item_id": "s1", "content": "c", "memory_type": "fa_snapshot", "metadata": {}}
    ]
    assert any("metadata" in m and "not a dict" in m for m in _messages(caplog))


def test_effects_valid_string_bundle_is_decoded_and_kept(registered, backend):
    backend.item = {
        "item_id": "s1",
        "content": "c",
        "memory_type": "fa_snapshot",
        "metadata": {
            "effects_bundle": json.dumps({"effects": [2]}),
            "owner_key": "OWNER_SECRET",
        },
    }
    result = registered["code_effects"](repo="r", source_snapshot="sha256:" + "a" * 64)
    assert result["items"][0]["metadata"] == {"effects_bundle": {"effects": [2]}}


def test_effects_non_dict_item_is_dropped_with_warning(registered, backend, caplog):
    backend.request = lambda *a, **k: {"items": ["RAW_SECRET"], "total": 1}
    with caplog.at_level(logging.WARNING, logger=LOGGER):
        result = registered["code_effects"](
            repo="r", source_snapshot="sha256:" + "a" * 64
        )
    assert result["items"] == []
    assert any("dropped an item" in m for m in _messages(caplog))


# ---- R2-3: session selectors filter raw items before compaction ---------- #


@pytest.mark.parametrize("selector", ["session_id", "conversation_id"])
@pytest.mark.parametrize("cite", [True, False])
def test_recall_session_selectors_match_before_compaction(
    registered, backend, selector, cite
):
    backend.item = {
        "item_id": "review-2",
        "content": "note",
        "memory_type": "pending",
        "metadata": {selector: "s1", "owner_key": "OWNER_SECRET"},
    }
    result = registered["memory_recall"](query="note", session_id="s1", cite=cite)
    if cite:
        assert [item["item_id"] for item in result["items"]] == ["review-2"]
        assert "session_id" not in result["items"][0]["metadata"]
    else:
        assert "review-2" in result
    _assert_clean(result)
    other = registered["memory_recall"](query="note", session_id="other", cite=True)
    assert other["items"] == []


def test_working_context_and_recall_keep_the_same_overflow(registered, backend):
    backend.item = {
        "item_id": "review-2",
        "content": "note",
        "memory_type": "pending",
        "metadata": {
            "conflicts": [{"existing_item_id": "o", "conflict_type": "t"}] * 50
        },
    }
    context = registered["get_working_context"](session_id="s1", query="note")
    recall = registered["memory_recall"](query="note", cite=True)
    assert context["items"][0]["metadata"]["conflicts_more"] == 30
    assert recall["items"][0]["metadata"]["conflicts_more"] == 30
