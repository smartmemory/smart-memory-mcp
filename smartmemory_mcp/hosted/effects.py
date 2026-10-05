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
        for item in result.get("items", []):
            metadata = item.get("metadata") or {}
            bundle = metadata.get("effects_bundle")
            if isinstance(bundle, str):
                try:
                    metadata["effects_bundle"] = json.loads(bundle)
                except ValueError as exc:
                    log.warning(
                        "Uploaded effects evidence corrupt, bundle could not be decoded"
                    )
                    raise ValueError("Corrupt uploaded effects bundle") from exc
        if not result.get("items"):
            log.warning(
                "Uploaded effects evidence unavailable for %s at %s, no source scan was performed",
                repo,
                source_snapshot,
            )
        return result
