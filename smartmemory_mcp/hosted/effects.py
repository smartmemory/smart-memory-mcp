"""Scoped stored effects read. No filesystem parameters or core imports."""

from __future__ import annotations

import hashlib
import json
import logging

from mcp.types import ToolAnnotations

log = logging.getLogger(__name__)


def register(mcp):
    """Replace the local scanner with an authenticated uploaded-snapshot reader."""
    from smartmemory_mcp.tools import common
    from smartmemory_mcp.tools.memory_tools import model_item

    @mcp.tool(
        title="Read uploaded Python effects",
        annotations=ToolAnnotations(
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False,
        ),
    )
    @common.graceful
    def code_effects(
        repo: str, source_snapshot: str, limit: int = 20, offset: int = 0
    ) -> dict:
        """Read fa_snapshot evidence uploaded to the selected authenticated workspace.

        Results are records, with full source-qualified effects in metadata.effects_bundle.
        Scan on the client, declare fa_snapshot through existing ontology APIs and
        ingest the snapshot_record payload. This tool never scans the server disk.
        """
        if (
            not repo
            or not source_snapshot.startswith("sha256:")
            or not 1 <= limit <= 100
            or offset < 0
        ):
            raise ValueError(
                "Effects requires repo, sha256 source_snapshot, limit 1..100 and nonnegative offset"
            )
        key = hashlib.sha256(
            json.dumps(
                [repo, source_snapshot],
                ensure_ascii=True,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        backend = common.get_backend()
        if not backend.supports("request"):
            log.warning(
                "Hosted backend lacks scoped HTTP reads, uploaded effects evidence was not obtained"
            )
            raise ValueError("Hosted effects requires authenticated remote backend")
        result = backend.request(
            "GET",
            "/memory/list",
            params={
                "memory_type": "fa_snapshot",
                "metadata_key": "effects_key",
                "metadata_value": key,
                "limit": limit,
                "offset": offset,
                "include_grounding": "false",
            },
        )
        if result.get("error"):
            log.warning(
                "Uploaded effects evidence could not be read: %s", result["error"]
            )
            raise ValueError(result["error"])
        # Items carry their own fields plus the requested effects bundle, never
        # other metadata (MCP-MEMGET-META-1).
        raw_items = result.get("items")
        items = []
        for item in raw_items if isinstance(raw_items, list) else []:
            if not isinstance(item, dict):
                log.warning(
                    "code_effects dropped a %s entry from its items: not a record dict",
                    type(item).__name__,
                )
                continue
            metadata = item.get("metadata") or {}
            if not isinstance(metadata, dict):
                log.warning(
                    "code_effects item %s has %s metadata, not a dict; its effects "
                    "bundle was not read",
                    item.get("item_id"),
                    type(metadata).__name__,
                )
                metadata = {}
            bundle = metadata.get("effects_bundle")
            if isinstance(bundle, str):
                try:
                    bundle = json.loads(bundle)
                except ValueError as exc:
                    log.warning(
                        "Uploaded effects evidence corrupt, bundle could not be decoded"
                    )
                    raise ValueError("Corrupt uploaded effects bundle") from exc
            out = model_item(item)
            if bundle is not None:
                out["metadata"] = {"effects_bundle": bundle}
            items.append(out)
        if isinstance(raw_items, list):
            result = {**result, "items": items}
        if not result.get("items"):
            log.warning(
                "Uploaded effects evidence unavailable for %s at %s, no source scan was performed",
                repo,
                source_snapshot,
            )
        return result
