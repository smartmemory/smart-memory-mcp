"""Structured ingestion MCP tools."""

import logging
from typing import Any, Dict

from .common import get_backend, graceful

logger = logging.getLogger(__name__)


def register(mcp):
    """Register structured ingestion tools with the MCP server (2 tools)."""

    @mcp.tool()
    @graceful
    def memory_ingest_structured(data: Dict[str, Any], schema_name: str) -> str:
        """Ingest structured data via a registered handler, bypassing NLP pipeline."""
        backend = get_backend()
        item_id = backend.ingest_structured(data, schema=schema_name)
        return f"Structured item ingested. Schema: {schema_name}, Item ID: {item_id}"

    @mcp.tool()
    @graceful
    def memory_ingest_document(
        source: str,
        source_type: str = "auto",
        chunk_size: int = 2000,
        chunk_strategy: str = "paragraph",
        reference: bool = False,
    ) -> str:
        """Ingest a document from a URL or file path.

        Creates a parent document node (INDEXED) and chunk nodes (FULL, embedded)
        linked via PART_OF edges. Deduplicates by source URL and content hash.

        Args:
            source: Public URL (http/https), or a file path in local mode only
                (.txt, .md, .pdf, .docx).
            source_type: "html" | "pdf" | "docx" | "txt" | "markdown" | "auto".
            chunk_size: Max characters per chunk (default 2000).
            chunk_strategy: "paragraph" | "sentence" | "markdown" | "recursive".
            reference: Mark nodes as reference material.
        """
        backend = get_backend()
        result = backend.ingest_document(
            source,
            source_type=source_type,
            chunk_size=chunk_size,
            chunk_strategy=chunk_strategy,
            reference=reference,
        )
        return (
            f"Document processed. ID: {result['document_id']}, "
            f"chunks: {len(result['chunk_ids'])}, status: {result['status']}"
        )
