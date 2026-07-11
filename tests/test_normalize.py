"""Tests for MemoryResult normalization."""

from smartmemory_mcp.backends.models import normalize_item, normalize_items


class TestNormalizeItem:
    """normalize_item() handles dict, object, and alias resolution."""

    def test_dict_passthrough(self):
        raw = {
            "item_id": "abc",
            "content": "hello",
            "memory_type": "semantic",
            "metadata": {"key": "val"},
            "score": 0.9,
        }
        result = normalize_item(raw)
        assert result["item_id"] == "abc"
        assert result["content"] == "hello"
        assert result["memory_type"] == "semantic"
        assert result["metadata"] == {"key": "val"}
        assert result["score"] == 0.9

    def test_id_alias_resolution(self):
        """'id' field maps to 'item_id' in result."""
        raw = {"id": "xyz", "content": "test", "memory_type": "episodic"}
        result = normalize_item(raw)
        assert result["item_id"] == "xyz"

    def test_item_id_takes_precedence_over_id(self):
        raw = {"item_id": "real", "id": "fallback", "content": "test"}
        result = normalize_item(raw)
        assert result["item_id"] == "real"

    def test_transaction_time_alias_resolution(self):
        """'transaction_time' maps to 'created_at'."""
        raw = {"content": "test", "transaction_time": "2026-03-27T00:00:00Z"}
        result = normalize_item(raw)
        assert result["created_at"] == "2026-03-27T00:00:00Z"

    def test_created_at_fallback(self):
        raw = {"content": "test", "created_at": "2026-01-01"}
        result = normalize_item(raw)
        assert result["created_at"] == "2026-01-01"

    def test_transaction_time_takes_precedence(self):
        raw = {
            "content": "test",
            "transaction_time": "newer",
            "created_at": "older",
        }
        result = normalize_item(raw)
        assert result["created_at"] == "newer"

    def test_missing_fields_get_defaults(self):
        result = normalize_item({})
        assert result["item_id"] == ""
        assert result["content"] == ""
        assert result["memory_type"] == "semantic"
        assert result["metadata"] == {}
        assert result["created_at"] == ""
        assert result["stale"] is False
        assert result.get("score") is None
        assert result.get("confidence") is None
        assert result.get("derived_from") is None

    def test_default_memory_type_override(self):
        result = normalize_item({}, default_type="episodic")
        assert result["memory_type"] == "episodic"

    def test_none_metadata_becomes_empty_dict(self):
        raw = {"content": "test", "metadata": None}
        result = normalize_item(raw)
        assert result["metadata"] == {}

    def test_object_with_to_dict(self):
        class FakeItem:
            def to_dict(self):
                return {
                    "item_id": "from-obj",
                    "content": "object content",
                    "memory_type": "procedural",
                    "metadata": {},
                }

        result = normalize_item(FakeItem())
        assert result["item_id"] == "from-obj"
        assert result["content"] == "object content"
        assert result["memory_type"] == "procedural"

    def test_plain_object_with_aliases(self):
        class LegacyItem:
            id = "legacy-id"
            content = "legacy content"
            memory_type = "pending"
            metadata = {"src": "test"}
            transaction_time = "2026-03-27"
            score = 0.75

        result = normalize_item(LegacyItem())
        assert result["item_id"] == "legacy-id"
        assert result["created_at"] == "2026-03-27"
        assert result["content"] == "legacy content"

    def test_optional_fields_preserved(self):
        raw = {
            "item_id": "x",
            "content": "y",
            "confidence": 0.85,
            "stale": True,
            "derived_from": "parent-id",
            "origin": "cli:add",
            "workspace_id": "workspace-1",
            "valid_start_time": "2026-01-01T00:00:00Z",
            "valid_end_time": "2026-12-31T00:00:00Z",
            "transaction_time": "2026-07-11T00:00:00Z",
            "reference": True,
            "entities": [{"name": "Alice"}],
            "relations": [{"type": "KNOWS"}],
            "drift_warnings": [{"severity": "high"}],
        }
        result = normalize_item(raw)
        assert result["confidence"] == 0.85
        assert result["stale"] is True
        assert result["derived_from"] == "parent-id"
        assert result["origin"] == "cli:add"
        # Server-only identity is never carried into the client-facing model.
        assert "workspace_id" not in result
        assert result["valid_start_time"] == "2026-01-01T00:00:00Z"
        assert result["valid_end_time"] == "2026-12-31T00:00:00Z"
        assert result["transaction_time"] == "2026-07-11T00:00:00Z"
        assert result["reference"] is True
        assert len(result["entities"]) == 1
        assert len(result["relations"]) == 1
        assert len(result["drift_warnings"]) == 1


class TestNormalizeItems:
    def test_empty_list(self):
        assert normalize_items([]) == []

    def test_list_of_dicts(self):
        raw = [
            {"item_id": "a", "content": "first"},
            {"item_id": "b", "content": "second"},
        ]
        results = normalize_items(raw)
        assert len(results) == 2
        assert results[0]["item_id"] == "a"
        assert results[1]["item_id"] == "b"

    def test_mixed_shapes(self):
        class Obj:
            def to_dict(self):
                return {"item_id": "obj", "content": "from object"}

        raw = [{"item_id": "dict", "content": "from dict"}, Obj()]
        results = normalize_items(raw)
        assert results[0]["item_id"] == "dict"
        assert results[1]["item_id"] == "obj"
