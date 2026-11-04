"""Local backend — delegates to smartmemory_app.storage (optional dependency)."""

from __future__ import annotations

import importlib
import logging
import sys
import threading
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, NoReturn

from smartmemory_mcp.tools.common import scoped_options

from .interface import LOCAL_BROKEN_TOOLS, BackendCapabilities, EvidenceRefused
from .models import MemoryResult, normalize_item, normalize_items
from smartmemory_mcp.workspace_select import SwitchResult, refuse

log = logging.getLogger(__name__)

# Lite runs one in-process store; it has no workspaces beyond this name.
LOCAL_WORKSPACE = "default"


def _metadata_matches(metadata: dict, key: str, value: Any) -> bool:
    """Exact metadata match, honouring the service's dotted-path syntax.

    Mirrors `/memory/list`'s `metadata_key` contract (`profile.tier` descends
    into nested dicts). The value always arrives as a string from an MCP tool
    argument, so comparison is string-based — which keeps bool and int distinct
    for free: `str(True) == "True"` never equals `str(1) == "1"`, so `flag=1`
    cannot match `flag=true`. That is the same bool/int divergence guarded on
    the service side, where Python's `True == 1` disagrees with type-aware
    FalkorDB. Only bools are case-folded, so `"true"` matches stored `True`.
    """
    cur: Any = metadata
    for part in key.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return False
        cur = cur[part]
    if isinstance(cur, bool):
        return str(cur).lower() == str(value).strip().lower()
    return str(cur) == str(value)


class _FrozenListPage(list):
    """A gate-ON list page: the plain list ``list_memories`` always returned, plus the walk's frozen ``total`` and the
    names (``partial``, as ``PartialRows``) of why core called it degraded. ``total`` is None when core gave none."""

    def __init__(
        self, rows: Any, *, total: int | None, partial: tuple[str, ...]
    ) -> None:
        super().__init__(rows)
        self.total = total
        self.partial = partial


class SaveReturnUnavailable(RuntimeError):
    """Core returned a save receipt but the installed wrapper cannot read it. No id was inferred."""


@dataclass(frozen=True)
class SaveReturn:
    """One core save return, read. ``value`` is what ``add`` returns (the id string, or the legacy held dict),
    ``receipt`` the wire receipt on the receipt arm and None on the legacy arm."""

    value: Any
    receipt: dict[str, Any] | None = None
    summary: str | None = None


#: Core's receipt classes, by name (mirrors ``smartmemory_app.save_return.RECEIPT_CLASS_NAMES``).
_RECEIPT_CLASS_NAMES = frozenset({"Receipt", "ReceiptRefusal"})


def _is_receipt_arm(value: Any) -> bool:
    """Whether a core save return is a save receipt (object or wire mapping) rather than a legacy shape.

    The wrapper's one rule (``smartmemory_app.save_return.is_receipt_arm``, U4.42 r2 N1, r3 F1), mirrored here so the
    legacy arm needs neither the wrapper nor core. Receipt identity is decided FIRST: an object whose class or a base is
    named ``Receipt``/``ReceiptRefusal``, or that is an instance of an already-imported ``smartmemory.save.receipt``'s
    classes, is a receipt even when it also is a ``str`` or a ``Mapping``. Only then: ``str`` and a mapping without
    ``schema_version`` are legacy, a mapping with it is a wire receipt. Never an exact class-name equality and never
    ``dict`` only. The wrapper's resolver then refuses any receipt-arm object that is not this core's receipt.
    """
    if any(
        getattr(cls, "__name__", None) in _RECEIPT_CLASS_NAMES
        for cls in type(value).__mro__
    ):
        return True
    try:  # read, never imported: a core that never loaded its receipts cannot have returned one
        module = sys.modules["smartmemory.save.receipt"]
    except KeyError:
        module = None
    classes = tuple(
        cls
        for cls in (
            getattr(module, "Receipt", None),
            getattr(module, "ReceiptRefusal", None),
        )
        if isinstance(cls, type)
    )
    if classes and isinstance(value, classes):
        return True
    if isinstance(value, str):
        return False
    if isinstance(value, Mapping):
        return "schema_version" in value
    return False


def _logged(exc: SaveReturnUnavailable) -> SaveReturnUnavailable:
    """Mark ``exc`` logged with core's own ``mark_refusal_logged``; a core without it has no boundary that reads one."""
    try:
        from smartmemory.refusal_logging import mark_refusal_logged
    except ImportError:
        return exc
    return mark_refusal_logged(exc)


def _receipt_unreadable(
    op: str, cause: BaseException | None = None
) -> SaveReturnUnavailable:
    """Log the one WARNING for a receipt this install cannot read and return the marked refusal to raise.

    A ``cause`` that already carries core's logged mark (an inspection failure its writer logged) adds no second WARNING.
    """
    if not _cause_logged(cause):
        log.warning(
            "Save return %s refused: smartmemory_app.save_return is not installed, so a save receipt "
            "cannot be read; no id returned",
            op,
        )
    return _logged(
        SaveReturnUnavailable(
            "Local save returned a receipt but the installed smartmemory wrapper cannot read it"
        )
    )


def _cause_logged(cause: BaseException | None) -> bool:
    """Whether ``cause`` already carries core's logged mark (``refusal_logged``): its writer logged the one WARNING."""
    if cause is None:
        return False
    try:
        from smartmemory.refusal_logging import refusal_logged
    except ImportError:  # a core without the mark has no marked cause
        return False
    return refusal_logged(cause)


def _resolve_save_return(value: Any, *, op: str) -> SaveReturn:
    """Receipt-or-legacy view of a core save return (N-02.activate.outer-readers).

    Every legacy return (id string, held or queued dict, anything else) is the legacy arm: returned as it is,
    the wrapper not imported. A receipt is read by the wrapper's one resolver, which raises a typed, logged
    refusal instead of turning a rejected or unreadable receipt into an id. A return that cannot be inspected is
    refused on that first failure (U4.42 r3 F3): it is never inspected again and never read as legacy.
    """
    try:
        receipt_arm = _is_receipt_arm(value)
    except Exception as exc:  # a return that cannot even be told apart is never an id: the wrapper's one refusal ends it
        try:
            from smartmemory_app.save_return import refuse_uninspectable
        except ImportError:
            raise _receipt_unreadable(
                op, exc
            ) from exc  # the inspection failure stays the cause
        refuse_uninspectable(op, exc)
    if not receipt_arm:
        return SaveReturn(value)
    try:
        from smartmemory_app.save_return import resolve_save_return
    except ImportError as exc:
        raise _receipt_unreadable(op) from exc
    resolved = resolve_save_return(value, op=op)
    return SaveReturn(resolved.value, resolved.receipt, resolved.summary)


def _save_value(value: Any, *, op: str) -> Any:
    return _resolve_save_return(value, op=op).value


# SM-EVIDENCE-TYPE-1: the contract's evidence_response fields, in contract order (the service route's shape).
_EVIDENCE_RESPONSE_FIELDS = (
    "evidence_id",
    "evidence_ref",
    "kind",
    "subject",
    "checker",
    "observed_at",
    "checked_at",
    "checks",
    "payload",
)


def _evidence_dict(evidence: Any) -> dict[str, Any]:
    """Serialize a core Evidence node to the service's evidence_response (node fields plus the Compose record)."""
    from smartmemory.evidence import to_record

    data = evidence.to_dict()
    body = {name: data.get(name) for name in _EVIDENCE_RESPONSE_FIELDS}
    body["record"] = to_record(evidence)
    return body


def _decision_dict(decision: Any) -> dict[str, Any]:
    """Serialize a core Decision to the same dict the REST routes return.

    Tolerates something that is already a dict so a caller-supplied double is
    not forced to re-implement `to_dict()`.
    """
    if isinstance(decision, dict):
        return decision
    return decision.to_dict()


def _set_fields(**kwargs: Any) -> dict[str, Any]:
    """The fields the caller set: core's create takes only what was given, never a None."""
    return {key: value for key, value in kwargs.items() if value is not None}


def _parse_expires_at(value: str) -> Any:
    """An ISO-8601 string to a timezone-aware datetime (a naive time is read as UTC, like the service route)."""
    from datetime import datetime, timezone

    parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class LocalBackend(BackendCapabilities):
    """Wraps smartmemory package for local-mode operations."""

    # Tools using REST have a separate local implementation. The rest are tools
    # that only error here (MCP-LOCAL-BROKEN-TOOLS-1, bug per tool in LOCAL_BROKEN_TOOLS;
    # empty since SM-LOCAL-TOOLS-1).
    unsupported_capabilities = frozenset({"request"}) | frozenset(LOCAL_BROKEN_TOOLS)

    def __init__(self) -> None:
        self._memory: Any = None
        self._init_lock = threading.Lock()

    def _get_memory(self) -> Any:
        """Resolve storage only when an operation needs the local memory."""
        try:
            from smartmemory_app.storage import get_memory
        except ImportError as exc:
            raise RuntimeError(
                "Local backend requires the smartmemory package.\nInstall with: pip install smartmemory"
            ) from exc
        return get_memory()

    @property
    def _mem(self) -> Any:
        """Initialize once under a lock, caching only successful construction."""
        if self._memory is None:
            with self._init_lock:
                if self._memory is None:
                    self._memory = self._get_memory()
        return self._memory

    @_mem.setter
    def _mem(self, memory: Any) -> None:
        """Allow explicit memory injection by existing adapters and tests."""
        self._memory = memory

    def ingest_code(self, **kwargs: Any) -> Any:
        """Delegate checkout indexing to core's shared parse/resolve/publish seam."""
        return self._mem.ingest_code(**kwargs)

    def code_dead_code(self, **kwargs: Any) -> dict[str, Any]:
        """Analyze stored callable kinds with the shared core candidate policy."""
        from smartmemory.code.dead_code import local_dead_code

        memory = self._mem
        with memory._di_context():
            return local_dead_code(memory._graph.backend, **kwargs)

    def code_dependencies(
        self,
        entity_name: str,
        direction: str = "both",
        repo: str | None = None,
        file_path: str | None = None,
        item_id: str | None = None,
    ) -> dict[str, Any]:
        """Resolve dependencies through scoped graph primitives, including Lite."""
        from .code_dependencies import local_code_dependencies

        memory = self._mem
        with memory._di_context():
            return local_code_dependencies(
                memory._graph, entity_name, direction, repo, file_path, item_id
            )

    # -- Core CRUD --

    def export_okf(self, bundle_path: str) -> int:
        """Export the active local workspace to an OKF bundle directory."""
        from smartmemory.corpus.exporter import CorpusExporter

        return CorpusExporter(self._mem).run(bundle_path)

    def import_okf(self, bundle_path: str) -> Any:
        """Losslessly import an OKF bundle through the direct add path."""
        from smartmemory.corpus.importer import CorpusImporter

        return CorpusImporter(self._mem, mode="direct").run(bundle_path)

    def add(
        self,
        content: str,
        memory_type: str = "semantic",
        metadata: dict | None = None,
        **kwargs: Any,
    ) -> str:
        """Store a memory item. Returns the id (or the legacy held dict), whichever arm core returns."""
        return _save_value(
            self._add_raw(content, memory_type, metadata, **kwargs), op="add"
        )

    def add_with_receipt(
        self,
        content: str,
        memory_type: str = "semantic",
        metadata: dict | None = None,
        **kwargs: Any,
    ) -> SaveReturn:
        """Store a memory item and keep the save receipt when core returns one (see ``SaveReturn``)."""
        return _resolve_save_return(
            self._add_raw(content, memory_type, metadata, **kwargs), op="add"
        )

    def _add_raw(
        self,
        content: str,
        memory_type: str,
        metadata: dict | None,
        **kwargs: Any,
    ) -> Any:
        """The core save, unread: an id string today, a receipt after the producer flip."""
        from smartmemory.models.memory_item import MemoryItem

        # DIST-LITE-QUIET-1: attribute the write so it lands as tier-2 user content, not
        # origin='unknown' (tier 4, hidden from recall+search). An explicit origin in
        # metadata (e.g. an importer) wins; default to this producer.
        meta = dict(metadata or {})
        context = dict(kwargs.pop("context", None) or {})
        meta.update({key: value for key, value in context.items() if key != "origin"})
        from smartmemory import resolve_origin

        origin = resolve_origin(
            context,
            origin=kwargs.pop("origin", None) or meta.pop("origin", None),
            default="mcp:memory_save",  # REL-CALLERS-ORIGIN-TAGS-1: was mcp:memory_add (tier 2 either way)
        )
        item = MemoryItem(
            content=content, memory_type=memory_type, metadata=meta, origin=origin
        )
        return self._mem.add(item)

    def get(self, item_id: str, **kwargs: Any) -> MemoryResult | None:
        """Retrieve a memory by ID."""
        result = self._mem.get(item_id)
        if result is None:
            return None
        return normalize_item(result)

    def explain(self, memory_id: str, **kwargs: Any) -> dict[str, Any] | None:
        """Full provenance answer via the core facade (PLAT-AUDITABLE-MEMORY-1)."""
        return self._mem.explain(memory_id)

    def update(
        self,
        item_id: str,
        content: str | None = None,
        metadata: dict | None = None,
        properties: dict | None = None,
        write_mode: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Update an existing memory via the canonical update_properties path.

        See CORE-CRUD-UPDATE-1 contract: `properties` takes precedence over the
        content/metadata conveniences; `write_mode` is "merge" (default) or
        "replace".
        """
        item = self._mem.get(item_id)
        if item is None:
            return f"Not found: {item_id}"

        if properties is not None:
            props = dict(properties)
        else:
            props = {}
            if content is not None:
                props["content"] = content
            if metadata is not None:
                existing_meta = {}
                if hasattr(item, "metadata") and isinstance(item.metadata, dict):
                    existing_meta = item.metadata
                elif isinstance(item, dict):
                    existing_meta = item.get("metadata") or {}
                props["metadata"] = {**existing_meta, **metadata}

        if not props:
            return "At least one of 'content', 'metadata', or 'properties' must be provided"

        self._mem.update_properties(item_id, props, write_mode=write_mode)
        return f"Updated: {item_id}"

    def supersede(
        self, item_id: str, content: str, reason: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Append a replacement via the core supersede primitive; the old item is kept."""
        from datetime import datetime, timezone

        from smartmemory.models.memory_item import MemoryItem

        old = self._mem.get(item_id)
        if old is None:
            raise RuntimeError(f"Not found: {item_id}")
        meta = {**(kwargs.get("metadata") or {}), "supersession_reason": reason}
        options = scoped_options(
            scope_go_lives=kwargs.get("scope_go_lives"),
            valid_from=kwargs.get("valid_from"),
        )
        valid_from = options.get("valid_from")
        start = None
        if valid_from:
            start = datetime.fromisoformat(valid_from)
            if start.tzinfo is None:
                start = start.replace(tzinfo=timezone.utc)
        item = MemoryItem(
            content=content,
            memory_type=kwargs.get("memory_type") or "semantic",
            metadata=meta,
            origin="mcp:memory_supersede",
            valid_start_time=start,
        )
        # The old item's interval closes at ``start`` inside core's compensated supersession unit.
        new_id = self._mem.ingest_superseding(
            item,
            supersedes=item_id,
            **({"valid_from": start} if start is not None else {}),
            **(
                {"scope_go_lives": options["scope_go_lives"]}
                if "scope_go_lives" in options
                else {}
            ),
        )
        if not new_id:
            raise RuntimeError("Supersession could not be established")
        replacement = self._mem.get(new_id)
        schedule = options.get("scope_go_lives")
        if schedule is None and replacement is not None:
            from smartmemory.temporal.scoped_validity import ScopeGoLives

            metadata = (
                replacement
                if isinstance(replacement, dict)
                else (replacement.metadata or {})
            )
            raw = getattr(replacement, "scope_go_lives", None) or metadata.get(
                "scope_go_lives"
            )
            schedule = (
                ScopeGoLives.from_value(raw).to_list() if raw is not None else None
            )
        if schedule is not None:
            return {
                "status": "scheduled",
                "old_item_id": item_id,
                "new_item_id": new_id,
                "scope_go_lives": schedule,
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
        return {
            "status": "superseded",
            "old_item_id": item_id,
            "new_item_id": new_id,
        }

    def delete(self, item_id: str, **kwargs: Any) -> bool:
        """Delete a memory by ID."""
        return self._mem.delete(item_id)

    # -- Search --

    def search(self, query: str, top_k: int = 5, **kwargs: Any) -> list[MemoryResult]:
        """Semantic search."""
        from smartmemory_app.storage import search

        results = normalize_items(search(query, top_k, **kwargs))
        # SELF-IMPROVE-6: track shown IDs for local-mode feedback
        self._last_search_session_id = f"local:{id(results)}:{top_k}"
        self._last_shown_ids = [
            r.get("item_id", "") for r in results if r.get("item_id")
        ]
        return results

    def search_by_metadata(
        self, metadata_key: str, metadata_value: str, top_k: int = 10, **kwargs: Any
    ) -> list[MemoryResult]:
        """Search via the public metadata facade; predicates run before LIMIT."""
        if not metadata_key:
            raise ValueError("metadata_key is required.")
        hits = self._mem.search_by_metadata(
            {metadata_key: metadata_value},
            top_k=top_k,
            **{k: kwargs[k] for k in ("since", "until") if kwargs.get(k) is not None},
        )
        return normalize_items(hits)

    def blame_code(self, **kwargs: Any) -> dict[str, Any]:
        """Code-provenance blame passthrough (CORE-CODE-PROVENANCE-1 Phase 2b).

        Delegates to the in-process lite SmartMemory, which reads the same local
        store the capture hook wrote to. Returns the forge-shaped BlameResult dict;
        raises ValueError on a git error (the tool maps it to a graceful string).
        """
        return self._mem.blame_code(**kwargs)

    def ask(
        self, question: str, limit: int = 5, *, reasoning: bool = True
    ) -> dict[str, Any]:
        """Delegate to the existing core answer implementation."""
        return self._mem.ask(question, limit=limit, reasoning=reasoning)

    def recall_pack(self, **kwargs: Any) -> dict[str, Any]:
        """Budgeted context assembly passthrough (CORE-RECALL-BUDGET-1).

        Delegates to the in-process SmartMemory facade, which owns the section gathering
        and the packing policy. Returns the RecallPack dict unchanged.
        """
        return self._mem.recall_pack(**kwargs)

    def policy_bundle(
        self,
        workflow: str | None = None,
        domain: str | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Compile the in-process store's policy bundle (as of ``as_of`` when given).

        Lite has one unscoped workspace (``LOCAL_WORKSPACE``); core honours that label only on an
        unscoped Lite store and refuses it on a scoped one (SM-LOCAL-TOOLS-1).
        """
        dated = {} if kwargs.get("as_of") is None else {"as_of": kwargs["as_of"]}
        return self._mem.compile_policy_bundle(
            workflow=workflow, domain=domain, workspace_id=LOCAL_WORKSPACE, **dated
        )

    def peer_chat(self, **kwargs: Any) -> dict[str, Any]:
        """Zero-schema peer synthesis passthrough (CORE-ZERO-SCHEMA-1 Phase 1).

        Delegates to the in-process lite SmartMemory, which owns the gather + single
        synthesis call. Local-only — no hosted REST route exists yet.
        """
        from smartmemory.peer.synthesis import peer_chat

        return peer_chat(self._mem, **kwargs)

    def read_transcript_centered(self, **kwargs: Any) -> dict[str, Any]:
        """Centered transcript-reader passthrough (CORE-CODE-PROVENANCE-1 Phase 2c).

        The read half of the blame->read chain. Delegates to the in-process lite
        SmartMemory, which reads the raw transcript JSONL on the local filesystem.
        Local-only — the hosted REST surface is parked.
        """
        return self._mem.read_transcript_centered(**kwargs)

    # -- Ingest & Recall --

    def ingest(self, content: str, memory_type: str = "episodic", **kwargs: Any) -> str:
        """Full pipeline ingestion. Returns what core returns, unread (dev tools still call this)."""
        return self._ingest_raw(content, memory_type, **kwargs)

    def ingest_with_receipt(
        self, content: str, memory_type: str = "episodic", **kwargs: Any
    ) -> SaveReturn:
        """Full pipeline ingestion that keeps the save receipt when core returns one (see ``SaveReturn``).

        Goes through the wrapper's receipt doorway (``storage.ingest_with_receipt``), never ``storage.ingest``, whose
        legacy return reads a receipt as its id. A wrapper without the doorway answers the legacy arm with one WARNING.
        """
        try:
            from smartmemory_app.storage import ingest_with_receipt
        except ImportError:
            log.warning(
                "Save return ingest: the installed smartmemory wrapper has no storage.ingest_with_receipt, "
                "so memory_save answers without the save receipt"
            )
            return _resolve_save_return(
                self._ingest_raw(content, memory_type, **kwargs), op="ingest"
            )
        saved = ingest_with_receipt(content, memory_type, **self._ingest_kwargs(kwargs))
        return SaveReturn(saved.value, saved.receipt, saved.summary)

    def _ingest_raw(self, content: str, memory_type: str, **kwargs: Any) -> Any:
        """The legacy full-pipeline save: what ``storage.ingest`` returns (an id string with ``sync`` left True)."""
        from smartmemory_app.storage import ingest

        return ingest(content, memory_type, **self._ingest_kwargs(kwargs))

    @staticmethod
    def _ingest_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
        """MCP save kwargs as ``storage.ingest`` keywords: ``properties`` and an attributed ``origin``."""
        kwargs = dict(kwargs)
        # storage.ingest() uses 'properties' not 'metadata'
        if "metadata" in kwargs:
            kwargs["properties"] = kwargs.pop("metadata")
        # DIST-LITE-QUIET-1: attribute the write (the /remember skill + MCP memory_ingest
        # surface) so it lands as tier-2 user content, not origin='unknown'. A caller-
        # supplied origin is preserved.
        from smartmemory import resolve_origin

        context = dict(kwargs.pop("context", None) or {})
        kwargs["origin"] = resolve_origin(
            context, origin=kwargs.get("origin"), default="mcp:memory_save"
        )
        context.pop("origin", None)
        if context:
            kwargs["properties"] = {**(kwargs.get("properties") or {}), **context}
        return kwargs

    def recall(self, cwd: str | None = None, top_k: int = 10, **kwargs: Any) -> str:
        """Context-aware recall."""
        from smartmemory_app.storage import recall

        return recall(cwd, top_k)

    def ingest_document(
        self,
        source: str,
        *,
        source_type: str = "auto",
        chunk_size: int = 2000,
        chunk_strategy: str = "paragraph",
        reference: bool = False,
    ) -> dict[str, Any]:
        """Ingest a URL or local file through the core document facade."""
        return self._mem.ingest_document(
            source,
            source_type=source_type,
            chunk_size=chunk_size,
            chunk_strategy=chunk_strategy,
            reference=reference,
        )

    def ingest_document_with_receipt(self, source: str, **kwargs: Any) -> SaveReturn:
        """Document ingestion that keeps the save receipt when core returns one (a legacy result dict is the legacy arm)."""
        return _resolve_save_return(
            self.ingest_document(source, **kwargs), op="ingest_document"
        )

    def ingest_conversation_with_receipt(
        self, turns: list, **kwargs: Any
    ) -> SaveReturn:
        """Conversation ingestion that keeps the save receipt when core returns one (a legacy result dict is the legacy arm)."""
        return _resolve_save_return(
            self.ingest_conversation_sync(turns, **kwargs), op="ingest_conversation"
        )

    def ingest_structured(
        self,
        data: dict,
        schema: str | None = None,
        schema_name: str | None = None,
        **kwargs: Any,
    ) -> str:
        """Structured data ingestion. Returns the id, whichever arm core returns (a receipt reads as its subject id)."""
        return _save_value(
            self._ingest_structured_raw(data, schema, schema_name),
            op="ingest_structured",
        )

    def ingest_structured_with_receipt(
        self,
        data: dict,
        schema: str | None = None,
        schema_name: str | None = None,
        **kwargs: Any,
    ) -> SaveReturn:
        """Structured data ingestion that keeps the save receipt when core returns one (see ``SaveReturn``)."""
        return _resolve_save_return(
            self._ingest_structured_raw(data, schema, schema_name),
            op="ingest_structured",
        )

    def _ingest_structured_raw(
        self, data: dict, schema: str | None, schema_name: str | None
    ) -> Any:
        """The core structured save, unread: an id string today, a receipt after the producer flip."""
        return self._mem.ingest_structured(data, schema=schema or schema_name)

    # -- Receipt save (C-01.activate.save.local-adapters) --
    # Dormant: no tool calls these. Core refuses ``SmartMemory.save`` with ``SaveNotEnabled`` until the common-receipt
    # facade is enabled (U20.S2); the legacy add/ingest above never reach it and it never falls back to them.

    #: This backend's registered producer namespace (core ``PRODUCER_NAMESPACES``), the G158 first-party identity.
    SAVE_NAMESPACE = "mcp.local"
    #: The retry-key prefix a minted key carries (``mcp:<uuid4 hex>``).
    SAVE_SEAM = "mcp"

    def save_call(
        self,
        content: Any = None,
        *,
        data: Any = None,
        memory_type: Any = None,
        extract: Any = None,
        wait: Any = None,
        requested_links: Any = None,
        context: Any = None,
        idempotency_key: Any = None,
    ) -> Any:
        """Build one frozen save call carrying a stable retry key: the supplied one verbatim, or a minted one.

        Submit the returned call again (``submit_save``) to retry with the same key and request. Every field is
        passed to core unjudged; only ``None`` means omitted.
        """
        build_save_call = self._save_caller("build_save_call")
        return build_save_call(
            seam=self.SAVE_SEAM,
            content=content,
            data=data,
            memory_type=memory_type,
            extract=extract,
            wait=wait,
            requested_links=requested_links,
            context=context,
            idempotency_key=idempotency_key,
            adapter_namespace=self.SAVE_NAMESPACE,
        )

    def submit_save(self, call: Any) -> SaveReturn:
        """Submit a ``save_call`` through ``SmartMemory.save`` and return the read receipt (see ``SaveReturn``).

        Core's typed refusals (``SaveNotEnabled``, ``SaveRequestError``, ``IncompatibleWait``) and the wrapper's
        (``SaveRejected``, ``SaveReturnRefused``, ``SaveCallRefused``) pass through unchanged, each already logged once.
        """
        try:
            from smartmemory_app.storage import save
        except ImportError as exc:
            self._save_unavailable(exc)
        resolved = save(call, memory=self._mem)
        return SaveReturn(resolved.value, resolved.receipt, resolved.summary)

    def save(self, content: Any = None, **kwargs: Any) -> SaveReturn:
        """Build and submit one receipt save. Keyword fields are those of ``save_call``."""
        return self.submit_save(self.save_call(content, **kwargs))

    def _save_caller(self, name: str) -> Any:
        try:
            caller = importlib.import_module("smartmemory_app.save_caller")
        except ImportError as exc:
            self._save_unavailable(exc)
        symbol = getattr(caller, name, None)
        if (
            symbol is None
        ):  # a wrapper whose save_caller lacks the export is the same unavailable seam
            self._save_unavailable(
                AttributeError(f"smartmemory_app.save_caller has no {name}")
            )
        return symbol

    @staticmethod
    def _save_unavailable(exc: BaseException) -> NoReturn:
        log.warning(
            "Save refused: the installed smartmemory wrapper has no receipt save (smartmemory_app.save_caller and "
            "storage.save), so a receipt save cannot be made; nothing was saved and add/ingest were not used"
        )
        raise _logged(
            SaveReturnUnavailable(
                "Local receipt save needs a smartmemory wrapper with save_caller; nothing was saved"
            )
        ) from exc

    def ingest_conversation_sync(
        self,
        turns: list,
        session_boundaries: list | None = None,
        conversation_id: str | None = None,
        session_dates: list | None = None,
        turns_per_chunk: int = 15,
        max_chunk_chars: int = 12000,
        max_concurrent: int = 4,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Conversation bulk ingestion via local SmartMemory (RLM-1g)."""
        from dataclasses import asdict

        response = self._mem.ingest_conversation_sync(
            turns,
            session_boundaries=session_boundaries,
            conversation_id=conversation_id,
            session_dates=session_dates,
            turns_per_chunk=turns_per_chunk,
            max_chunk_chars=max_chunk_chars,
            max_concurrent=max_concurrent,
        )
        return (
            asdict(response) if hasattr(response, "__dataclass_fields__") else response
        )

    def read_around(
        self,
        item_id: str,
        char_budget: int = 20000,
        before_ratio: float = 0.3,
        after_ratio: float = 0.7,
        cursor: dict | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Auto-centered conversation read via local SmartMemory (CORE-RECALL-CENTERED-1 P2).

        Returns a char-budgeted asymmetric window of the conversation chunks
        surrounding ``item_id``. Hard-truncates (raise_on_overflow=False) rather
        than raising, so the MCP tool always returns a window.
        """
        return self._mem.read_around(
            item_id,
            char_budget=char_budget,
            before_ratio=before_ratio,
            after_ratio=after_ratio,
            cursor=cursor,
            raise_on_overflow=False,
        )

    # -- Listing & Stats --

    def list_memories(
        self, limit: int = 100, offset: int = 0, **kwargs: Any
    ) -> list[MemoryResult]:
        """List memories with pagination, optionally filtered by metadata.

        `smartmemory.SmartMemory` has no `list_memories` — this delegation
        raised AttributeError on every local-mode call (verified 2026-08-02).
        Listing is served from a frozen walk over core's always-on read gate
        (``_list_frozen``), paginated locally.
        """
        mkey, mval = kwargs.get("metadata_key"), kwargs.get("metadata_value")
        if (mkey is None) != (mval is None):
            missing = "metadata_value" if mkey is not None else "metadata_key"
            raise ValueError(
                f"metadata_key and metadata_value must be supplied together; {missing} is missing."
            )

        return self._list_frozen(limit, offset, mkey, mval)

    # LOCAL-FROZEN-PAGE-1 (R11(d)): a list walk is frozen by core's ``read_gated_roots_page``. ``memory_list``
    # keeps its ``limit``/``offset`` contract (no cursor reaches a client), so the backend remembers, per filter, the
    # cursor that continues each page it served: ``offset`` N resumes the walk whose last page ended at N.
    _WALK_FILTERS_KEPT = 8
    _WALK_OFFSETS_KEPT = 64

    def _list_frozen(
        self, limit: int, offset: int, mkey: str | None, mval: Any
    ) -> list[MemoryResult]:
        """One page of the list from a frozen walk: membership and ``total`` are fixed at the walk's first page."""
        from collections import OrderedDict

        from smartmemory.refusal_logging import mark_refusal_logged
        from smartmemory.search.direct_lists import REFUSALS
        from smartmemory.search.visibility_pages import CursorRejected

        if limit < 1:
            return _FrozenListPage(
                [], total=None, partial=()
            )  # inherited: limit=0 is accepted, an empty page
        walks = self.__dict__.setdefault("_walks", OrderedDict())
        key = (mkey, None if mval is None else str(mval))
        offsets: dict[int, str] = walks.setdefault(key, {})
        walks.move_to_end(key)
        while len(walks) > self._WALK_FILTERS_KEPT:
            walks.popitem(last=False)
        accept = None
        discriminator = ""
        if mkey is not None:
            discriminator = f"metadata:{mkey}={mval}"

            def accept(row: Mapping[str, Any], _ctx: Any) -> bool:
                return _metadata_matches(
                    normalize_item(dict(row)).get("metadata") or {}, mkey, mval
                )

        # Offset 0 is a new look at the list, so it starts a new walk. A later offset resumes the walk that served the
        # page before it, or, with no such page on record, starts a walk and skips ``offset`` rows of it.
        cursor = None if offset == 0 else offsets.get(offset)
        if offset == 0:
            offsets.clear()
        for attempt in (0, 1):
            skip = offset if cursor is None and offset > 0 else 0
            try:
                page = self._mem.read_gated_roots_page(
                    limit=skip + limit,
                    cursor=cursor,
                    accept=accept,
                    discriminator=discriminator,
                    include_total=True,
                )
                break
            except CursorRejected as exc:
                if attempt:
                    # Core logged this rejection where it raised it: the tool must not log it again.
                    raise mark_refusal_logged(exc)
                log.warning(
                    "list_memories lost its frozen walk at offset %d (%s); the page is cut from a new walk, so its "
                    "membership and total may differ from the earlier pages.",
                    offset,
                    exc,
                )
                offsets.clear()
                cursor = None
            except REFUSALS as exc:
                # Core's typed refusals are logged once where raised (direct_lists docstring: callers log nothing more).
                raise mark_refusal_logged(exc)
        rows = page.rows[skip:]
        if page.next_cursor:
            offsets[offset + len(rows)] = page.next_cursor
            while len(offsets) > self._WALK_OFFSETS_KEPT:
                offsets.pop(next(iter(offsets)))
        partial = ("read_gate_scan_budget_spent",) if page.degraded else ()
        try:
            rows = [self._with_read_status(raw) for raw in rows]
        except REFUSALS as exc:
            raise mark_refusal_logged(exc)
        return _FrozenListPage(
            [normalize_item(raw) for raw in rows],
            total=page.total_visible,
            partial=partial,
        )

    @staticmethod
    def _with_read_status(raw: Mapping[str, Any]) -> dict[str, Any]:
        """A frozen-walk row with core's response-only ``read_status`` (U5.22), built from the stored node alone.

        The roots walk lists graph roots and reads no admission record, so a row's status is the graph row's: merged
        intake status for a landed row is U5.8's (``direct_lists`` docstring).
        """
        from smartmemory.models.read_status import READ_STATUS_KEY, read_status
        from smartmemory.search.pending_facade import extraction_status

        row = dict(raw)
        if (
            READ_STATUS_KEY in row
        ):  # a carrier core already attached is kept, and normalize_item refuses it if unreadable
            return row
        revision = 1 if row.get("revision") is None else row["revision"]
        row[READ_STATUS_KEY] = read_status(extraction_status(row, None), revision)
        return row

    def clear_user_memories(self, confirm: bool = False, **kwargs: Any) -> str:
        """Clear all user memories.

        Core exposes `clear()`, not `clear_user_memories` — the old delegation
        raised AttributeError, so this tool never cleared anything in local mode
        (verified 2026-08-02 against the core facade).
        """
        if not confirm:
            return "Pass confirm=True to clear all memories."
        self._mem.clear()
        return "All memories cleared."

    def get_all_items_debug(self, **kwargs: Any) -> dict:
        """Get debug stats."""
        return self._mem.get_all_items_debug()

    def stats(self, **kwargs: Any) -> dict:
        """Memory statistics."""
        return self.get_all_items_debug(**kwargs)

    # -- Evolution --

    def run_evolution_cycle(self, **kwargs: Any) -> dict:
        """Trigger evolution cycle."""
        return self._mem.run_evolution_cycle(**kwargs)

    # CORE-MEMORY-DYNAMICS-1 M1b: commit_working_to_* removed — core façades
    # are gone, ConsolidationRouter routes at ingest. Use add()/ingest() with
    # memory_type="pending" instead.

    def run_evolver(self, evolver_name: str, **kwargs: Any) -> dict:
        """Run a specific evolver by name."""
        return self._mem.run_evolver(evolver_name, **kwargs)

    def run_clustering(self, **kwargs: Any) -> dict:
        """Run clustering."""
        return self._mem.run_clustering(**kwargs)

    # -- Insight --

    def reflect(self, **kwargs: Any) -> str:
        """Reflective analysis."""
        return self._mem.reflect(**kwargs)

    def summary(self, **kwargs: Any) -> dict:
        """Memory summary."""
        return self._mem.summary(**kwargs)

    def orphaned_notes(self, **kwargs: Any) -> list[MemoryResult]:
        """Find orphaned notes."""
        return normalize_items(self._mem.orphaned_notes(**kwargs))

    def find_old_notes(self, days: int = 90, **kwargs: Any) -> list[MemoryResult]:
        """Find notes older than the given number of days."""
        return normalize_items(self._mem.find_old_notes(days, **kwargs))

    def personalize(
        self,
        user_id: str = "mcp-user",
        traits: dict | None = None,
        preferences: dict | None = None,
        **kwargs: Any,
    ) -> str:
        """Personalize memory system."""
        return self._mem.personalize(
            user_id=user_id,
            traits=traits or {},
            preferences=preferences or {},
            **kwargs,
        )

    def update_from_feedback(
        self, feedback: dict | None = None, memory_type: str = "semantic", **kwargs: Any
    ) -> str:
        """Update from user feedback."""
        return self._mem.update_from_feedback(
            feedback=feedback or {}, memory_type=memory_type, **kwargs
        )

    def ground(self, item_id: str, **kwargs: Any) -> dict:
        """Ground a memory item."""
        return self._mem.ground(item_id=item_id, **kwargs)

    # -- Graph --

    def link(
        self,
        source_id: str,
        target_id: str,
        link_type: str = "RELATES_TO",
        **kwargs: Any,
    ) -> str:
        """Link two memories."""
        return self._mem.link(source_id, target_id, link_type=link_type)

    def add_edge(
        self, source_id: str, target_id: str, relation_type: str, **kwargs: Any
    ) -> str:
        """Add a graph edge."""
        return self._mem.add_edge(
            source_id, target_id, relation_type=relation_type, **kwargs
        )

    def get_links(self, item_id: str, **kwargs: Any) -> list[MemoryResult]:
        """Get links for an item."""
        return normalize_items(self._mem.get_links(item_id))

    def get_neighbors(self, item_id: str, **kwargs: Any) -> dict:
        """Get graph neighbors."""
        return self._mem.get_neighbors(item_id)

    def find_shortest_path(self, start_id: str, end_id: str, **kwargs: Any) -> list:
        """Find shortest path between two items."""
        return self._mem.find_shortest_path(start_id, end_id, **kwargs)

    # -- Auth (no-ops for local) --

    def login(self, api_key: str, **kwargs: Any) -> str:
        """No-op in local mode."""
        return "Local mode — no authentication required."

    def whoami(self, **kwargs: Any) -> str:
        """Local mode session info."""
        return (
            "Local mode — single user.\n"
            "Tenant: none (local mode has no tenants)\n"
            f"Workspace: {LOCAL_WORKSPACE} (Lite has no other workspaces)\n"
            "Backend: local"
        )

    def resolve_switch(self, workspace: str) -> SwitchResult:
        """Same answer as `switch_workspace`: Lite has nothing to resolve or commit."""
        return self.switch_workspace(workspace)

    def set_active_workspace(self, workspace_id: str) -> None:
        """No-op: Lite's only workspace is `default`, and `resolve_switch` refuses others."""

    def switch_workspace(self, workspace: str, **kwargs: Any) -> SwitchResult:
        """Lite has one workspace, `default`; any other name is refused."""
        if (workspace or "").strip().casefold() == LOCAL_WORKSPACE:
            return SwitchResult(
                ok=True,
                message=(
                    f"Already in workspace: {LOCAL_WORKSPACE}. "
                    "Lite (local mode) has no other workspaces."
                ),
                workspace_id=LOCAL_WORKSPACE,
            )
        return refuse(
            f"Lite (local mode) has no workspaces; only {LOCAL_WORKSPACE!r} exists. "
            "Use the remote backend to work across workspaces.",
            LOCAL_WORKSPACE,
        )

    # -- Retrieval feedback (SELF-IMPROVE-6) --

    def submit_feedback(
        self, search_session_id: str, result_used: list[str], **kwargs: Any
    ) -> dict:
        """Emit result-selection feedback via core retrieval_tracking (local mode).

        Uses the shown_ids captured from the most recent search() call to provide
        accurate selection rate data (not just result_used == result_shown).
        """
        try:
            from smartmemory.observability.retrieval_tracking import (
                emit_result_feedback,
            )

            # Use shown_ids from the search that produced these results
            shown_ids = getattr(self, "_last_shown_ids", None) or []
            if not shown_ids:
                log.warning(
                    "submit_feedback: no prior search() call — shown_ids unknown, skipping"
                )
                return {"status": "skipped", "reason": "no prior search context"}

            emit_result_feedback(
                query_hash="",
                result_used=result_used,
                result_shown=shown_ids,
                workspace_id="default",
                search_session_id=search_session_id,
            )
            return {
                "status": "ok",
                "search_session_id": search_session_id,
                "result_used_count": len(result_used),
            }
        except Exception as exc:
            return {"error": str(exc)}

    # -- Learned lessons and constraints (SM-LEARNED-TOOLS-1) --
    #
    # Core's managers take the REAL SmartMemory (``self._mem``), never this wrapper (the decision
    # precedent below): the managed-type write calls ``sm.add``, which the wrapper would re-wrap.

    def add_learning(
        self,
        content: str,
        *,
        category: str | None = None,
        incident_link: str | None = None,
        learned_scope: str | None = None,
    ) -> dict[str, Any]:
        """``SmartMemory.add_learning`` on the real store, serialized to the route's response shape."""
        learned = self._mem.add_learning(
            content,
            **_set_fields(
                category=category,
                incident_link=incident_link,
                learned_scope=learned_scope,
            ),
        )
        return {
            "item_id": learned.learned_id,
            "learned_id": learned.learned_id,
            "content": learned.content,
            "category": getattr(learned, "category", None),
            "incident_link": getattr(learned, "incident_link", None),
            "learned_scope": getattr(learned, "learned_scope", None),
        }

    def add_constraint(
        self,
        content: str,
        *,
        domain: str | None = None,
        source: str | None = None,
        expires_at: str | None = None,
    ) -> dict[str, Any]:
        """``SmartMemory.add_constraint`` on the real store, serialized to the route's response shape."""
        fields = _set_fields(domain=domain, source=source, expires_at=expires_at)
        expires_at = fields.get("expires_at")
        if isinstance(expires_at, str):
            fields["expires_at"] = _parse_expires_at(expires_at)
        constraint = self._mem.add_constraint(content, **fields)
        expires = getattr(constraint, "expires_at", None)
        return {
            "item_id": constraint.constraint_id,
            "constraint_id": constraint.constraint_id,
            "content": constraint.content,
            "domain": getattr(constraint, "domain", None),
            "source": getattr(constraint, "source", None),
            "expires_at": expires.isoformat()
            if hasattr(expires, "isoformat")
            else expires,
        }

    # -- Decision lifecycle (MCP-REMOTE-DECISIONS-1) --
    #
    # The managed-type Managers/Queries take the REAL SmartMemory (``self._mem``),
    # never this wrapper: ``DecisionManager`` calls ``sm.add(MemoryItem)`` and
    # ``sm._graph``, and passing the wrapper silently re-wrapped the MemoryItem as
    # ``content`` — a successful-looking write that discarded decision_type,
    # confidence, rationale and the deterministic id (2026-06-02 bug hunt).
    # Living here rather than in the tool is what lets the tool stay
    # transport-agnostic; the ``_mem`` requirement is unchanged.

    def _decision_manager(self):
        from smartmemory.decisions.manager import DecisionManager

        return DecisionManager(self._mem)

    def _decision_queries(self):
        from smartmemory.decisions.queries import DecisionQueries

        return DecisionQueries(self._mem)

    def _residuation(self):
        from smartmemory.reasoning.residuation import ResiduationManager

        return ResiduationManager(self._mem)

    def decision_create(self, content: str, **kwargs: Any) -> dict[str, Any]:
        """DecisionManager.create, serialized to the wire shape."""
        decision = self._decision_manager().create(
            content=content,
            decision_type=kwargs.get("decision_type", "inference"),
            confidence=kwargs.get("confidence", 0.8),
            source_trace_id=kwargs.get("source_trace_id"),
            evidence_ids=kwargs.get("evidence_ids") or [],
            domain=kwargs.get("domain"),
            tags=kwargs.get("tags") or [],
            rejected_alternatives=kwargs.get("rejected_alternatives") or [],
            rationale=kwargs.get("rationale"),
            constraints=kwargs.get("constraints") or [],
            **scoped_options(scope_go_lives=kwargs.get("scope_go_lives")),
        )
        return _decision_dict(decision)

    def decision_get(
        self,
        decision_id: str,
        as_of: str | None = None,
        business_scope: dict[str, Any] | None = None,
        valid_at: str | None = None,
    ) -> dict[str, Any] | None:
        options = scoped_options(business_scope=business_scope, valid_at=valid_at)
        # The facade also resolves scheduled history when both qualifiers are omitted.
        decision = self._mem.get_decision(decision_id, as_of=as_of, **options)
        return _decision_dict(decision) if decision else None

    def decision_list(
        self,
        domain: str | None = None,
        decision_type: str | None = None,
        min_confidence: float = 0.0,
        limit: int = 50,
        as_of: str | None = None,
        business_scope: dict[str, Any] | None = None,
        valid_at: str | None = None,
    ) -> list[dict[str, Any]]:
        options = scoped_options(business_scope=business_scope, valid_at=valid_at)
        decisions = self._mem.get_active_decisions(
            domain=domain,
            decision_type=decision_type,
            min_confidence=min_confidence,
            limit=limit,
            as_of=as_of,
            **options,
        )
        return [_decision_dict(d) for d in decisions]

    def decision_search(
        self,
        topic: str,
        limit: int = 20,
        as_of: str | None = None,
        business_scope: dict[str, Any] | None = None,
        valid_at: str | None = None,
    ) -> list[dict[str, Any]]:
        options = scoped_options(business_scope=business_scope, valid_at=valid_at)
        if as_of is not None or options:
            decisions = self._mem.get_decisions_about(
                topic=topic, limit=limit, as_of=as_of, **options
            )
        else:
            decisions = self._decision_queries().get_decisions_about(
                topic=topic, limit=limit
            )
        return [_decision_dict(d) for d in decisions]

    def decision_supersede(
        self,
        decision_id: str,
        new_content: str,
        reason: str,
        new_decision_type: str = "inference",
        new_confidence: float = 0.8,
        rejected_alternatives: list[str] | None = None,
        rationale: str | None = None,
        constraints: list[str] | None = None,
        scope_go_lives: list[dict[str, Any]] | None = None,
        valid_from: str | None = None,
    ) -> dict[str, Any] | None:
        from smartmemory.models.decision import Decision

        options = scoped_options(scope_go_lives=scope_go_lives, valid_from=valid_from)
        new_decision = Decision(
            content=new_content,
            decision_type=new_decision_type,
            confidence=new_confidence,
        )
        try:
            result = self._decision_manager().supersede(
                decision_id,
                new_decision,
                reason=reason,
                rejected_alternatives=rejected_alternatives,
                rationale=rationale,
                constraints=constraints,
                **options,
            )
        except ValueError as error:
            # Only actual missing IDs are absence. Typed schedule/conflict errors escape.
            if type(error) is ValueError and str(error).startswith(
                "Decision not found:"
            ):
                return None
            raise
        schedule = getattr(result, "scope_go_lives", None)
        if schedule is not None:
            from datetime import datetime, timezone

            return {
                "status": "scheduled",
                "old_item_id": decision_id,
                "new_item_id": result.decision_id,
                "scope_go_lives": schedule.to_list(),
                "recorded_at": datetime.now(timezone.utc).isoformat(),
            }
        return {
            "old_decision_id": decision_id,
            "new_decision_id": result.decision_id,
            "status": "superseded",
        }

    def decision_retract(self, decision_id: str, reason: str) -> dict[str, Any] | None:
        try:
            self._decision_manager().retract(decision_id, reason=reason)
        except ValueError:
            return None
        return {"decision_id": decision_id, "status": "retracted"}

    def decision_reinforce(
        self, decision_id: str, evidence_id: str
    ) -> dict[str, Any] | None:
        try:
            decision = self._decision_manager().reinforce(decision_id, evidence_id)
        except ValueError:
            return None
        return {
            "decision_id": decision_id,
            "confidence": decision.confidence,
            "reinforcement_count": decision.reinforcement_count,
        }

    def decision_contradict(
        self, decision_id: str, evidence_id: str
    ) -> dict[str, Any] | None:
        try:
            decision = self._decision_manager().contradict(decision_id, evidence_id)
        except ValueError:
            return None
        return {
            "decision_id": decision_id,
            "confidence": decision.confidence,
            "contradiction_count": decision.contradiction_count,
        }

    def decision_link_evidence(
        self, decision_id: str, evidence_id: str
    ) -> dict[str, Any]:
        """SmartMemory.link_decision_evidence on the real store; core's refusal is re-raised as EvidenceRefused."""
        try:
            decision = self._mem.link_decision_evidence(decision_id, evidence_id)
        except ValueError as e:
            raise EvidenceRefused(str(e)) from e
        # The link resolved an ev: ref to its node id; read it back so the answer names the node it linked.
        linked = self._mem.get_evidence(evidence_id)
        if linked is None:
            raise RuntimeError(
                f"Linked evidence {evidence_id!r} could not be read back"
            )
        return {
            "decision_id": decision_id,
            "evidence_id": linked.evidence_id,
            "evidence_ids": list(decision.evidence_ids),
            "confidence": decision.confidence,
        }

    def evidence_record(self, record: dict[str, Any]) -> dict[str, Any]:
        """SmartMemory.add_evidence on the real store (SM-EVIDENCE-TYPE-1)."""
        try:
            evidence = self._mem.add_evidence(record)
        except ValueError as e:
            raise EvidenceRefused(str(e)) from e
        return _evidence_dict(evidence)

    def decision_provenance(
        self,
        decision_id: str,
        as_of: str | None = None,
        business_scope: dict[str, Any] | None = None,
        valid_at: str | None = None,
    ) -> dict[str, Any] | None:
        options = scoped_options(business_scope=business_scope, valid_at=valid_at)
        if as_of is not None or options:
            provenance = self._mem.get_decision_provenance(
                decision_id, as_of=as_of, **options
            )
        else:
            provenance = self._decision_queries().get_decision_provenance(decision_id)
        if not provenance or provenance.get("decision") is None:
            return None
        return {
            "decision": _decision_dict(provenance["decision"]),
            "reasoning_trace": provenance.get("reasoning_trace"),
            "evidence": provenance.get("evidence") or [],
            "superseded": [
                _decision_dict(d) for d in (provenance.get("superseded") or [])
            ],
            "superseded_by": [
                _decision_dict(d) for d in (provenance.get("superseded_by") or [])
            ],
        }

    def decision_find_conflicts(self, decision_id: str) -> dict[str, Any] | None:
        manager = self._decision_manager()
        decision = manager.get_decision(decision_id)
        if not decision:
            return None
        conflicts = manager.find_conflicts(decision)
        return {
            "decision_id": decision_id,
            "conflicts": [_decision_dict(c) for c in conflicts],
            "count": len(conflicts),
        }

    def decision_create_pending(
        self,
        content: str,
        requirements: list[dict[str, Any]],
        domain: str | None = None,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        decision = self._residuation().create_pending(
            content=content,
            requirements=requirements,
            domain=domain,
            tags=tags or [],
        )
        return _decision_dict(decision)

    def decision_resolve_requirement(
        self, decision_id: str, requirement_id: str, memory_id: str
    ) -> bool:
        return bool(
            self._residuation().resolve_requirement(
                decision_id, requirement_id, memory_id
            )
        )

    def decision_try_activate(self, decision_id: str) -> bool:
        return bool(self._residuation().try_activate(decision_id))

    # -- Patterns, plans, anchors, failure journal (SM-LOCAL-TOOLS-1) --
    #
    # Same rule as the decision block above: the core managers take the REAL SmartMemory
    # (``self._mem``), never this wrapper. Handed the wrapper they read ``memory._graph``
    # (AttributeError) or call ``memory.add(MemoryItem)``, which this wrapper's
    # ``add(content: str)`` received as text. Plain dicts, lists and strings come out, so a
    # remote implementation can return the same shapes.

    def _pattern_manager(self):
        from smartmemory.adherence import PatternManager

        return PatternManager(self._mem)

    def _plan_manager(self):
        from smartmemory.plans.manager import PlanManager

        return PlanManager(self._mem)

    def _anchor_manager(self):
        from smartmemory.anchors.manager import AnchorManager

        return AnchorManager(self._mem)

    def _failure_journal(self):
        from smartmemory.plans.failure_journal import FailureJournal

        return FailureJournal(self._mem)

    def pattern_query(
        self,
        scope: str | None = None,
        severity: str | None = None,
        free_text: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        patterns = self._pattern_manager().query(
            scope=scope, severity=severity, free_text=free_text, limit=limit
        )
        return [_pattern_dict(p) for p in patterns]

    def pattern_get(self, pattern_id: str) -> dict[str, Any] | None:
        pattern = self._pattern_manager().get(pattern_id)
        return None if pattern is None else _pattern_dict(pattern)

    def pattern_list(self, limit: int = 200) -> list[dict[str, Any]]:
        return [
            _pattern_dict(p)
            for p in self._pattern_manager().queries.list_all(limit=limit)
        ]

    # Pattern writes (SM-PATTERN-CRUD-1, OWNER D-0089). Every write carries this process's
    # provenance: ``source`` on create, ``updated_by`` on update, ``mcp:<agent id>`` when
    # the store's scope is an agent, else ``mcp:local``.

    def _pattern_editor(self) -> str:
        provider = getattr(self._mem, "scope_provider", None)
        resolve = getattr(provider, "resolve_agent_id", None)
        agent = resolve() if callable(resolve) else None
        return f"mcp:{agent or 'local'}"

    def pattern_create(
        self,
        title: str,
        content: str,
        severity: str = "warn",
        scope: list[str] | None = None,
        examples: list[dict[str, Any]] | None = None,
        rationale: str = "",
    ) -> dict[str, Any]:
        editor = self._pattern_editor()
        pattern = self._pattern_manager().create(
            content=content,
            title=title,
            severity=severity,
            scope=list(scope or []),
            examples=list(examples or []),
            rationale=rationale,
            source=editor,
            updated_by=editor,
        )
        return _pattern_dict(pattern)

    def pattern_update(
        self, pattern_id: str, /, **fields: Any
    ) -> dict[str, Any] | None:
        manager = self._pattern_manager()
        if manager.get(pattern_id) is None:
            return None
        fields["updated_by"] = self._pattern_editor()
        return _pattern_dict(manager.update(pattern_id, **fields))

    def pattern_retire(
        self, pattern_id: str, reason: str = "retired via MCP"
    ) -> dict[str, Any] | None:
        manager = self._pattern_manager()
        if manager.get(pattern_id) is None:
            return None
        manager.delete(pattern_id, reason=f"{reason} ({self._pattern_editor()})")
        return {"pattern_id": pattern_id, "status": "retracted"}

    def pattern_seed(self, directory: str) -> dict[str, Any]:
        from pathlib import Path

        from smartmemory.adherence import read_memory_dir

        index_text, files, refused = read_memory_dir(Path(directory) / "MEMORY.md")
        editor = self._pattern_editor()
        return self._mem.seed_patterns(
            index_text,
            files,
            refused=refused,
            source_prefix=f"{editor}:memory_md",
            updated_by=editor,  # as pattern_create stamps it
        )

    def plan_create(
        self,
        title: str,
        tasks: list[dict[str, Any]] | None = None,
        context: str = "",
        created_by: str = "",
    ) -> dict[str, Any]:
        """``{"plan_id", "task_ids"}`` from PlanManager.create."""
        return self._plan_manager().create(title, tasks or [], context, created_by)

    def plan_get(self, plan_id: str) -> dict[str, Any] | None:
        return self._plan_manager().get(plan_id)

    def plan_active(self) -> list[dict[str, Any]]:
        return self._plan_manager().get_active()

    def plan_update_task(self, plan_id: str, task_id: str, status: str) -> None:
        self._plan_manager().update_task(plan_id, task_id, status)

    def anchor_set(self, content: str, anchor_type: str, session_id: str) -> str:
        return self._anchor_manager().set(content, anchor_type, session_id)

    def anchor_list(
        self, session_id: str = "", anchor_type: str | None = None
    ) -> list[dict[str, Any]]:
        return self._anchor_manager().list(session_id, anchor_type)

    def anchor_clear(self, session_id: str = "", anchor_type: str | None = None) -> int:
        return self._anchor_manager().clear(session_id, anchor_type)

    def anchor_graduate(self, session_id: str = "") -> list[str]:
        return self._anchor_manager().graduate(session_id)

    def anchor_check_drift(
        self, session_id: str, recent_outputs: list[str]
    ) -> list[dict[str, Any]]:
        """Each core DriftReport as a dict (anchor_id, anchor_content, drift_score, missing_keywords, severity)."""
        from dataclasses import asdict

        reports = self._anchor_manager().check_drift(session_id, recent_outputs)
        return [asdict(report) for report in reports]

    def log_failure(
        self, error_type: str, content: str, context: str, **kwargs: Any
    ) -> str:
        """FailureJournal.log on the real store; returns the stored item id."""
        return self._failure_journal().log(
            error_type=error_type, content=content, context=context, **kwargs
        )

    def check_failure(
        self, error_type: str, context: str, top_k: int = 3
    ) -> list[dict[str, Any]]:
        return self._failure_journal().check_before_retry(error_type, context, top_k)


def _pattern_dict(pattern: Any) -> dict[str, Any]:
    """A core Pattern as a JSON-friendly dict."""
    if hasattr(pattern, "to_dict"):
        return pattern.to_dict()
    return dict(pattern)
