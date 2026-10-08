"""Local dependency resolution with the service's exact-or-dotted-suffix name policy."""

import json
from typing import Any


def local_code_dependencies(
    graph: Any,
    entity_name: str,
    direction: str,
    repo: str | None,
    file_path: str | None,
    item_id: str | None,
) -> dict[str, Any]:
    """Use owned nodes and edges, refusing ambiguity before traversing any candidate."""
    from smartmemory.code.models import code_evidence_properties

    if direction not in {"both", "dependencies", "dependents"}:
        return {"error": "direction must be both, dependencies or dependents"}
    workspace = graph.get_scope_filters().get("workspace_id")
    nodes = {
        node["item_id"]: node
        for node in graph.get_all_nodes_scoped()
        if node.get("memory_type") == "code"
        and (not workspace or node.get("workspace_id") == workspace)
    }
    lowered = entity_name.lower()
    matches = sorted(
        (
            node
            for node in nodes.values()
            if any(
                str(node.get(key) or "").lower() == lowered
                or str(node.get(key) or "").lower().endswith("." + lowered)
                for key in ("name", "qualified_name")
            )
            and (not repo or node.get("repo") == repo)
            and (not file_path or node.get("file_path") == file_path)
            and (not item_id or node.get("item_id") == item_id)
        ),
        key=lambda node: (
            node.get("repo", ""),
            node.get("file_path", ""),
            node.get("line_number", 0),
            node["item_id"],
        ),
    )

    def summary(node):
        return {
            key: node.get(key, "")
            for key in (
                "item_id",
                "name",
                "entity_type",
                "file_path",
                "line_number",
                "repo",
            )
        }

    if not matches:
        return {"error": f"Code entity '{entity_name}' not found"}
    if len(matches) != 1:
        return {
            "error": json.dumps(
                {
                    "status": "ambiguous",
                    "entity_name": entity_name,
                    "message": "Pass file_path or item_id from a candidate to choose one.",
                    "candidates": [summary(node) for node in matches[:50]],
                    "candidate_count": min(len(matches), 50),
                    "truncated": len(matches) > 50,
                }
            )
        }
    root = matches[0]
    root_id = root["item_id"]
    result = {
        "status": "resolved",
        "root": {**summary(root), **code_evidence_properties(root)},
        "dependents": [],
        "dependencies": [],
    }
    for edge in graph.get_edges_for_node(root_id):
        for label, endpoint, neighbour in (
            ("dependents", "target_id", "source_id"),
            ("dependencies", "source_id", "target_id"),
        ):
            node = nodes.get(edge[neighbour])
            if (
                edge[endpoint] == root_id
                and node
                and direction in {"both", label}
                and len(result[label]) < 50
            ):
                result[label].append(
                    {
                        **summary(node),
                        "edge_type": edge["edge_type"],
                        **code_evidence_properties(node),
                    }
                )
    return result
