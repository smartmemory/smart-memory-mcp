"""The save tools, by input (G56/G140, U4.42-FU-TOOLS): ``memory_save``, ``memory_save_document``,
``memory_save_conversation``.

They replace the six ingest-shaped tools (``memory_ingest``, ``memory_add``, ``memory_distill``,
``memory_ingest_conversation``, ``memory_ingest_structured``, ``memory_ingest_document``), which are gone with no
aliases. The agent picks the input, never an extraction knob. Every tool returns a ``ToolResult``: a one-line summary
as text and, as structured content, ``status`` (``saved|held|queued|failed``), the saved id, the receipt (or None)
and the receipt's summary. Both return arms are read: the legacy id/dict core returns today and the save receipt it
returns after the producer flip (``backends/receipts.py`` for the service wire, ``LocalBackend`` for in-process).
"""

import logging
from typing import Any, Dict, List, Optional

from fastmcp.exceptions import ToolError
from fastmcp.tools import ToolResult
from mcp.types import ToolAnnotations

from smartmemory_mcp.backends.local import SaveReturn

from .common import get_backend, graceful

logger = logging.getLogger(__name__)

#: Memory types stored raw, with no extraction pipeline. ``pending`` is the distill type: a conversation turn pair
#: waits for later recall, it is not entity-extracted at save time (old ``memory_distill`` behaviour).
RAW_MEMORY_TYPES = frozenset({"pending"})

_TURN_METADATA = {"role": "turn", "distill_version": "1"}

_SAVE_ANNOTATIONS = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    idempotentHint=False,
    openWorldHint=False,
)


def _format_turn_content(user_turn: str, assistant_turn: str) -> str:
    """Format a user/assistant turn pair for storage."""
    return f"USER: {user_turn.strip()}\nASSISTANT: {assistant_turn.strip()}"


def _turn_role(turn: Any) -> Optional[str]:
    if not isinstance(turn, dict):
        return None
    role = turn.get("role") or turn.get("speaker")
    return str(role).lower() if role else None


def _is_turn_pair(
    turns: list, session_boundaries: Optional[list], session_dates: Optional[list]
) -> bool:
    """Exactly one user turn then one assistant turn, with no boundaries or dates: the old distill input."""
    return (
        len(turns) == 2
        and not session_boundaries
        and not session_dates
        and _turn_role(turns[0]) == "user"
        and _turn_role(turns[1]) == "assistant"
        and all(isinstance(t.get("content"), str) for t in turns)
    )


def _held_fields(value: Dict[str, Any]) -> Dict[str, Any]:
    return {"reason": value.get("reason"), "reasons": value.get("reasons", [])}


def _receipt_status(receipt: Dict[str, Any]) -> str:
    """``saved|queued|failed`` from a receipt. A non-terminal save is queued, a failed or dead one failed."""
    state, outcome = receipt.get("execution_state"), receipt.get("outcome")
    if outcome in ("failed", "dead") or state in ("failed", "dead"):
        return "failed"
    return "saved" if receipt.get("terminal") else "queued"


def _item_result(saved: SaveReturn, *, noun: str, detail: str = "") -> ToolResult:
    """One item save (or one item-shaped pipeline result) as a ``ToolResult``. ``detail`` follows ``<noun> saved.``"""
    label = f"{noun} saved.{detail}"
    value, receipt = saved.value, saved.receipt
    fields: Dict[str, Any] = {"receipt": receipt, "summary": saved.summary}
    if receipt is not None:
        fields["item_id"] = str(value)
        fields["status"] = _receipt_status(receipt)
        return ToolResult(content=f"{noun}: {saved.summary}", structured_content=fields)
    if isinstance(value, dict):
        if value.get("status") == "held":
            return ToolResult(
                content=f"{noun} held: {value.get('reason')}. Details: {value.get('reasons', [])}",
                structured_content={**fields, "status": "held", **_held_fields(value)},
            )
        if "error" in value:
            return ToolResult(
                content=f"{noun} save failed: {value['error']}",
                structured_content={
                    **fields,
                    "status": "failed",
                    "error": str(value["error"]),
                },
            )
        item_id = value.get("item_id") or value.get("id")
        if not item_id:
            logger.warning(
                "%s: the backend returned no item id (%r); reporting none rather than inventing one",
                noun,
                value,
            )
        return ToolResult(
            content=f"{label} Item ID: {item_id}"
            if item_id
            else f"{label} No item id returned",
            structured_content={
                **fields,
                "status": "saved",
                "item_id": item_id,
                "result": value,
            },
        )
    return ToolResult(
        content=f"{label} Item ID: {value}",
        structured_content={**fields, "status": "saved", "item_id": str(value)},
    )


def _document_result(saved: SaveReturn) -> ToolResult:
    value, receipt = saved.value, saved.receipt
    fields: Dict[str, Any] = {"receipt": receipt, "summary": saved.summary}
    if receipt is not None:
        fields.update(status=_receipt_status(receipt), document_id=str(value))
        return ToolResult(
            content=f"Document: {saved.summary}", structured_content=fields
        )
    if not isinstance(value, dict) or not value.get("document_id"):
        raise RuntimeError(f"Document save returned no document id: {value!r}")
    chunks = len(value.get("chunk_ids") or [])
    return ToolResult(
        content=f"Document processed. ID: {value['document_id']}, chunks: {chunks}, status: {value.get('status')}",
        structured_content={
            **fields,
            "status": "saved",
            "document_id": value["document_id"],
            "chunk_ids": list(value.get("chunk_ids") or []),
            "document_status": value.get("status"),
        },
    )


def _conversation_result(
    saved: SaveReturn, conversation_id: Optional[str]
) -> ToolResult:
    value, receipt = saved.value, saved.receipt
    fields: Dict[str, Any] = {
        "receipt": receipt,
        "summary": saved.summary,
        "conversation_id": conversation_id,
    }
    if receipt is not None:
        fields["status"] = _receipt_status(receipt)
        return ToolResult(
            content=f"Conversation: {saved.summary}", structured_content=fields
        )
    if not isinstance(value, dict):
        raise RuntimeError(
            f"Conversation save returned an unreadable result: {value!r}"
        )
    if "error" in value:
        return ToolResult(
            content=f"Conversation save failed: {value['error']}",
            structured_content={
                **fields,
                "status": "failed",
                "error": str(value["error"]),
            },
        )
    held = [c for c in value.get("chunk_results") or [] if c.get("status") == "held"]
    saved_n = value.get("chunks_ingested", 0)
    failed_n = value.get("chunks_failed", 0) - len(held)
    status = (
        "failed"
        if failed_n and not saved_n
        else "held"
        if held and not saved_n
        else "saved"
    )
    message = f"Conversation {value.get('conversation_id', '')}: {saved_n} chunks ingested, {failed_n} failed."
    if held:
        message += f" {len(held)} held: " + ", ".join(
            str(c.get("reason")) for c in held
        )
    return ToolResult(
        content=message,
        structured_content={
            **fields,
            "status": status,
            "conversation_id": value.get("conversation_id") or conversation_id,
            "chunks_ingested": saved_n,
            "chunks_failed": failed_n,
            "chunks_held": len(held),
        },
    )


def register_free(mcp):
    """Register the FREE-tier save tool."""

    @mcp.tool(title="Save a memory", annotations=_SAVE_ANNOTATIONS)
    @graceful
    def memory_save(
        content: Optional[str] = None,
        memory_type: Optional[str] = None,
        metadata: Optional[Dict[str, Any]] = None,
        tags: Optional[List[str]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> ToolResult:
        """Save one memory: free text in ``content``, or a structured record in ``data``.

        Free text goes through the full extraction pipeline, whatever its type, except ``memory_type="pending"``,
        which is stored as is. For ``data``, ``memory_type`` names the registered schema (required) and ``content`` is
        not allowed. Returns the saved id, the save receipt and a one-line summary. ``status`` is saved, held (the
        memory was held for review: see ``reason``), queued (still processing) or failed.
        """
        if (content is None) == (data is None):
            raise ToolError("Provide exactly one of content or data.")
        backend = get_backend()
        if data is not None:
            if not memory_type:
                raise ToolError("data needs memory_type: the registered schema name.")
            if metadata or tags:
                raise ToolError("metadata and tags apply to content, not data.")
            saved = backend.ingest_structured_with_receipt(data, schema=memory_type)
            return _item_result(
                saved, noun="Structured item", detail=f" Schema: {memory_type}."
            )
        item_metadata = dict(metadata or {})
        if tags:
            item_metadata["tags"] = list(tags)
        mtype = memory_type or "semantic"
        kwargs = {"metadata": item_metadata} if item_metadata else {}
        if mtype in RAW_MEMORY_TYPES:
            saved = backend.add_with_receipt(content, memory_type=mtype, **kwargs)
        else:
            saved = backend.ingest_with_receipt(content, memory_type=mtype, **kwargs)
        return _item_result(saved, noun="Memory")


def register_pro(mcp):
    """Register the PRO-tier save tools (document and conversation)."""

    @mcp.tool(title="Save a document", annotations=_SAVE_ANNOTATIONS)
    @graceful
    def memory_save_document(
        source: str,
        source_type: str = "auto",
        chunk_size: int = 2000,
        chunk_strategy: str = "paragraph",
        reference: bool = False,
    ) -> ToolResult:
        """Save a document from a URL or file path.

        Creates a parent document node (INDEXED) and chunk nodes (FULL, embedded) linked via PART_OF edges.
        Deduplicates by source URL and content hash.

        Args:
            source: Public URL (http/https), or a file path in local mode only (.txt, .md, .pdf, .docx).
            source_type: "html" | "pdf" | "docx" | "txt" | "markdown" | "auto".
            chunk_size: Max characters per chunk (default 2000).
            chunk_strategy: "paragraph" | "sentence" | "markdown" | "recursive".
            reference: Mark nodes as reference material.
        """
        saved = get_backend().ingest_document_with_receipt(
            source,
            source_type=source_type,
            chunk_size=chunk_size,
            chunk_strategy=chunk_strategy,
            reference=reference,
        )
        return _document_result(saved)

    @mcp.tool(title="Save a conversation", annotations=_SAVE_ANNOTATIONS)
    @graceful
    def memory_save_conversation(
        turns: list,
        conversation_id: Optional[str] = None,
        session_boundaries: Optional[list] = None,
        session_dates: Optional[list] = None,
        turns_per_chunk: int = 15,
        max_chunk_chars: int = 12000,
    ) -> ToolResult:
        """Save one conversation turn pair or a whole transcript.

        Exactly two turns (a user turn then an assistant turn, no boundaries or dates) are stored as one pending
        memory for later recall. Anything else is a transcript: grouped into sessions (by explicit boundaries or
        auto-chunking), each chunk run through the full extraction pipeline, under one conversation container.

        Args:
            turns: List of dicts with "role"/"speaker" and "content" keys.
            conversation_id: User-supplied ID (auto-generated for a transcript if None).
            session_boundaries: Turn indices where sessions start (e.g. [0, 50, 120]).
            session_dates: ISO date per session for metadata.
            turns_per_chunk: Max turns per auto-chunk (default 15).
            max_chunk_chars: Safety split for oversized chunks (default 12000).
        """
        backend = get_backend()
        if _is_turn_pair(turns, session_boundaries, session_dates):
            meta: Dict[str, Any] = dict(_TURN_METADATA)
            if conversation_id:
                meta["conversation_id"] = conversation_id
            saved = backend.add_with_receipt(
                _format_turn_content(turns[0]["content"], turns[1]["content"]),
                memory_type="pending",
                metadata=meta,
            )
            return _item_result(saved, noun="Turn")
        saved = backend.ingest_conversation_with_receipt(
            turns,
            session_boundaries=session_boundaries,
            conversation_id=conversation_id,
            session_dates=session_dates,
            turns_per_chunk=turns_per_chunk,
            max_chunk_chars=max_chunk_chars,
        )
        return _conversation_result(saved, conversation_id)
