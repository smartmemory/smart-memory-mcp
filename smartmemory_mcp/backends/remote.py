"""RemoteBackend — httpx client to the SmartMemory hosted API.

Extracted from server.py. Session state is instance-level (not module globals).
_request() never raises — returns error dicts on all failure modes.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx

from .models import MemoryResult, normalize_item, normalize_items


class RemoteBackend:
    """HTTP client implementing MemoryBackend protocol for the hosted SmartMemory API."""

    def __init__(
        self,
        api_url: str | None = None,
        api_key: str | None = None,
        team_id: str | None = None,
    ) -> None:
        self._api_url = (
            api_url
            or os.environ.get("SMARTMEMORY_API_URL", "https://api.smartmemory.ai")
        ).rstrip("/")
        self._session: dict[str, str | bool] = {
            "access_token": api_key or os.environ.get("SMARTMEMORY_API_KEY", ""),
            "refresh_token": "",
            "team_id": team_id
            or os.environ.get(
                "SMARTMEMORY_TEAM_ID", os.environ.get("SMARTMEMORY_WORKSPACE_ID", "")
            ),
            "user_email": "",
            "_bootstrapped": False,
        }

    # --- Session bootstrap -------------------------------------------------------

    def _bootstrap_from_api_key(self) -> None:
        """On first use, call /auth/me to discover user identity and default team."""
        if self._session["_bootstrapped"] or not self._session["access_token"]:
            return
        self._session["_bootstrapped"] = True
        try:
            r = httpx.get(
                f"{self._api_url}/auth/me",
                headers={
                    "Authorization": f"Bearer {self._session['access_token']}",
                    "Content-Type": "application/json",
                },
                timeout=10,
            )
            if r.status_code == 200:
                user = r.json()
                self._session["user_email"] = user.get("email", "")
                if not self._session["team_id"]:
                    discovered = user.get("default_team_id") or ""
                    if discovered:
                        self._session["team_id"] = discovered
        except Exception:
            pass  # bootstrap is best-effort

    def _headers(self, workspace_id: str | None = None) -> dict[str, str]:
        """Build request headers with auth and workspace context."""
        self._bootstrap_from_api_key()
        return {
            "Authorization": f"Bearer {self._session['access_token']}",
            "Content-Type": "application/json",
            "X-Workspace-Id": workspace_id or str(self._session.get("team_id", "")),
        }

    def _request(
        self,
        method: str,
        path: str,
        workspace_id: str | None = None,
        timeout: int = 30,
        **kwargs: Any,
    ) -> Any:
        """Execute an API request. Never raises — returns error dict on any failure."""
        try:
            headers = kwargs.pop("headers", None) or self._headers(workspace_id)
            r = httpx.request(
                method,
                f"{self._api_url}{path}",
                headers=headers,
                timeout=timeout,
                **kwargs,
            )
            r.raise_for_status()
            return r.json() if r.status_code != 204 else None
        except httpx.ConnectError:
            return {
                "error": f"SmartMemory API unreachable at {self._api_url}. Check SMARTMEMORY_API_URL."
            }
        except httpx.HTTPStatusError as e:
            return {"error": f"API error {e.response.status_code}: {e.response.text}"}
        except Exception as e:
            return {"error": f"Request failed: {e}"}

    def request(self, method: str, path: str, **kwargs: Any) -> Any:
        """Public request method for tools that need REST calls not in the protocol."""
        return self._request(method, path, **kwargs)

    @property
    def active_workspace_id(self) -> str:
        """Return the workspace carried by the active remote session."""
        self._bootstrap_from_api_key()
        return str(self._session.get("team_id", ""))

    def export_okf(self, archive_path: str) -> None:
        """Stream the active workspace's OKF archive to disk."""
        destination = Path(archive_path)
        destination.parent.mkdir(parents=True, exist_ok=True)
        workspace_id = self.active_workspace_id
        try:
            with httpx.stream(
                "GET",
                f"{self._api_url}/memory/okf/export",
                headers=self._headers(workspace_id=workspace_id),
                timeout=120,
            ) as response:
                response.raise_for_status()
                with destination.open("wb") as handle:
                    for chunk in response.iter_bytes():
                        handle.write(chunk)
        except httpx.HTTPStatusError as exc:
            destination.unlink(missing_ok=True)
            raise RuntimeError(
                f"API error {exc.response.status_code}: {exc.response.text}"
            ) from exc
        except Exception:
            destination.unlink(missing_ok=True)
            raise

    def import_okf(self, archive_path: str) -> dict[str, Any]:
        """Upload an OKF archive into the active workspace."""
        workspace_id = self.active_workspace_id
        headers = self._headers(workspace_id=workspace_id)
        headers.pop("Content-Type", None)
        path = Path(archive_path)
        with path.open("rb") as handle:
            result = self._request(
                "POST",
                "/memory/okf/import",
                workspace_id=workspace_id,
                timeout=300,
                headers=headers,
                files={"file": (path.name, handle, "application/gzip")},
            )
        if err := self._fmt_error(result):
            raise RuntimeError(err)
        return result or {}

    @staticmethod
    def _fmt_error(result: Any) -> str | None:
        """If result is an error dict, return the message. Otherwise None."""
        if isinstance(result, dict) and "error" in result:
            return result["error"]
        return None

    # --- Auth --------------------------------------------------------------------

    def login(self, api_key: str | None = None, team_id: str | None = None) -> str:
        """Set API key and discover user identity from /auth/me."""
        key = api_key or os.environ.get("SMARTMEMORY_API_KEY", "")
        if not key:
            return "No API key. Pass api_key or set SMARTMEMORY_API_KEY env var."
        self._session["access_token"] = key
        self._session["refresh_token"] = ""
        self._session["user_email"] = ""
        self._session["_bootstrapped"] = False
        try:
            r = httpx.get(
                f"{self._api_url}/auth/me",
                headers={
                    "Authorization": f"Bearer {key}",
                    "Content-Type": "application/json",
                },
                timeout=15,
            )
            r.raise_for_status()
            user = r.json()
            self._session["user_email"] = user.get("email", "")
            self._session["team_id"] = (
                team_id or user.get("default_team_id") or str(self._session["team_id"])
            )
            self._session["_bootstrapped"] = True
        except httpx.HTTPStatusError as e:
            return f"API key validation failed ({e.response.status_code}): {e.response.text}"
        except Exception as e:
            return f"Login failed: {e}"
        return f"Logged in as {self._session['user_email']}, team: {self._session['team_id']}"

    def whoami(self) -> str:
        """Return current session info."""
        if not self._session["access_token"]:
            return (
                f"Not authenticated. API: {self._api_url}. Call login to authenticate."
            )
        return (
            f"User: {self._session['user_email'] or '(API key auth)'}\n"
            f"Team: {self._session['team_id']}\n"
            f"API: {self._api_url}\n"
            f"Backend: remote"
        )

    def switch_team(self, team_id: str) -> str:
        """Switch to a different team without re-authenticating."""
        self._session["team_id"] = team_id
        return f"Switched to team: {team_id}. User: {self._session['user_email']}"

    # --- MemoryBackend protocol: implemented (have REST routes) ------------------

    def add(self, content: str, memory_type: str = "semantic", **kwargs: Any) -> str:
        """POST /memory/add. Returns item_id string."""
        body: dict[str, Any] = {"content": content, "memory_type": memory_type}
        if metadata := kwargs.get("metadata"):
            body["metadata"] = (
                metadata if isinstance(metadata, dict) else json.loads(metadata)
            )
        if kwargs.get("use_pipeline"):
            body["use_pipeline"] = True
        result = self._request("POST", "/memory/add", json=body) or {}
        if isinstance(result, dict):
            # Surface the failure. The `str(result)` fallback below used to swallow
            # an {"error": ...} response and hand back "{'error': 'API error 500:
            # ...'}" AS THE NEW ITEM ID — reporting a failed write as a successful
            # one, with a fake id the caller could then store or look up.
            if err := self._fmt_error(result):
                raise RuntimeError(err)
            item_id = result.get("item_id") or result.get("id")
            if not item_id:
                raise RuntimeError(f"/memory/add returned no item id: {result!r}")
            return str(item_id)
        return str(result)

    def get(self, item_id: str, **kwargs: Any) -> MemoryResult | None:
        """GET /memory/{item_id}. Returns None only when the item genuinely is absent."""
        result = self._request("GET", f"/memory/{item_id}")
        if result is None:
            return None
        # A 404 IS genuine absence — keep returning None for it. But an API 403/500
        # must NOT be reported as "Memory item not found." Collapsing an outage into
        # absence is the silent degradation this repo's rules forbid: the caller
        # concludes the memory does not exist and may re-create it.
        if err := self._fmt_error(result):
            if "404" in err:
                return None
            raise RuntimeError(err)
        return normalize_item(result)

    def explain(self, memory_id: str, **kwargs: Any) -> dict[str, Any] | None:
        """GET /memory/{memory_id}/explain (PLAT-AUDITABLE-MEMORY-1).

        Same absence semantics as get(): 404 is genuine absence (None); any
        other API error raises rather than masquerading as absence.
        """
        result = self._request("GET", f"/memory/{memory_id}/explain")
        if result is None:
            return None
        if err := self._fmt_error(result):
            if "404" in err:
                return None
            raise RuntimeError(err)
        return result

    def update(self, item_id: str, **kwargs: Any) -> dict[str, Any]:
        """PUT /memory/{item_id}.

        Supports the CORE-CRUD-UPDATE-1 contract: content/metadata/properties/write_mode
        are forwarded verbatim. memory_type is forwarded for legacy compatibility.
        """
        body: dict[str, Any] = {}
        for key in ("content", "metadata", "properties", "write_mode", "memory_type"):
            if key in kwargs:
                body[key] = kwargs[key]
        return self._request("PUT", f"/memory/{item_id}", json=body) or {}

    def delete(self, item_id: str, **kwargs: Any) -> bool:
        """DELETE /memory/{item_id}. Returns True on success."""
        result = self._request("DELETE", f"/memory/{item_id}")
        if result is None:
            return True  # 204 No Content = success
        if isinstance(result, dict) and self._fmt_error(result):
            return False
        return True

    def search(self, query: str, top_k: int = 5, **kwargs: Any) -> list[MemoryResult]:
        """POST /memory/search."""
        body: dict[str, Any] = {"query": query, "top_k": top_k}
        if kwargs.get("enable_hybrid", True):
            body["enable_hybrid"] = True
        # Accept both decompose and decompose_query
        decompose = kwargs.get("decompose") or kwargs.get("decompose_query")
        if decompose:
            body["decompose"] = True
        for key in ("memory_type", "include_reference"):
            if key in kwargs and kwargs[key]:
                body[key] = kwargs[key]
        # RLM-1c: forward multi-hop params
        if kwargs.get("multi_hop"):
            body["multi_hop"] = True
            if "max_hops" in kwargs:
                body["max_hops"] = kwargs["max_hops"]
            if "budget_ms" in kwargs:
                body["budget_ms"] = kwargs["budget_ms"]
        # PLAT-AUDITABLE-MEMORY-1: as-of recall params (3rd forwarding point —
        # blueprint C3: a missed allowlist entry is a silent param drop).
        if kwargs.get("as_of_date"):
            body["as_of_date"] = kwargs["as_of_date"]
        if kwargs.get("include_superseded"):
            body["include_superseded"] = True
        # SELF-IMPROVE-6: capture X-Search-Session-Id header from response
        self._last_search_session_id: str | None = None
        try:
            r = httpx.request(
                "POST",
                f"{self._api_url}/memory/search",
                headers=self._headers(),
                json=body,
                timeout=30,
            )
            r.raise_for_status()
            self._last_search_session_id = r.headers.get(
                "x-search-session-id"
            ) or r.headers.get("X-Search-Session-Id")
            result = r.json() if r.status_code != 204 else None
        except httpx.ConnectError:
            result = {
                "error": f"SmartMemory API unreachable at {self._api_url}. Check SMARTMEMORY_API_URL."
            }
        except httpx.HTTPStatusError as e:
            result = {"error": f"API error {e.response.status_code}: {e.response.text}"}
        except Exception as e:
            result = {"error": f"Request failed: {e}"}
        # Surface backend errors instead of returning the error dict as if it were
        # a memory item (which mis-renders / KeyErrors downstream). @graceful turns
        # this into a clean tool error.
        if isinstance(result, dict):
            if err := self._fmt_error(result):
                raise RuntimeError(err)
            # CORE-RECALL-LINEAGE-1 SearchResponse envelope: {"results": [...], ...}.
            # Pre-LINEAGE-1 callers expecting a bare array must read .results —
            # treating the envelope as "not a list" silently emptied every search.
            rows = result.get("results")
            raw = rows if isinstance(rows, list) else []
        else:
            # Pre-LINEAGE-1 services returned a bare top-level array.
            raw = result if isinstance(result, list) else []
        return normalize_items(raw)

    def search_by_metadata(
        self, metadata_key: str, metadata_value: str, top_k: int = 10, **kwargs: Any
    ) -> list[MemoryResult]:
        """GET /memory/by-metadata — exact metadata match."""
        # Service caps limit at 200 (crud.py get_memory_by_metadata); top_k was
        # previously dropped entirely, silently pinning every call to the default 25.
        params = {
            "metadata_key": metadata_key,
            "metadata_value": metadata_value,
            "limit": str(max(1, min(top_k, 200))),
        }
        result = self._request("GET", "/memory/by-metadata", params=params)
        if isinstance(result, dict):
            # Surface the error rather than returning [error_dict] as a fake item.
            if err := self._fmt_error(result):
                raise RuntimeError(err)
            # The service returns the {"items": [...], "count": N} envelope, never a
            # bare item. Wrapping the envelope as one item made normalize_item's
            # all-.get()-with-defaults path emit a single BLANK memory and discard
            # every real hit — silent, unraisable, and indistinguishable from a
            # genuine one-result match. Same class as the LINEAGE-1 bug in search().
            rows = result.get("items")
            raw = rows if isinstance(rows, list) else []
        else:
            # Pre-GRAPH-API-1l services returned a bare top-level array.
            raw = result if isinstance(result, list) else []
        return normalize_items(raw)

    def recall(self, cwd: str | None = None, top_k: int = 10, **kwargs: Any) -> str:
        """Client-side recall — no /memory/recall endpoint in the hosted API."""
        requested = max(1, top_k)
        recent_k = max(1, (requested + 1) // 2)
        semantic_k = max(0, requested - recent_k)
        recent = self.search("", top_k=recent_k)
        semantic = (
            self.search(cwd or "", top_k=semantic_k) if cwd and semantic_k else []
        )
        seen: set[str] = set()
        items: list[MemoryResult] = []
        for r in recent + semantic:
            iid = r["item_id"]
            if iid and iid not in seen:
                seen.add(iid)
                items.append(r)
        recall_floor = float(os.environ.get("SMARTMEMORY_RECALL_FLOOR", "0.3"))
        items = [
            r
            for r in items
            if (r.get("confidence") if r.get("confidence") is not None else 1.0)
            >= recall_floor
        ]
        items = [r for r in items if not r.get("reference")]
        if not items:
            return ""
        lines = ["## SmartMemory Context"]
        for item in items[:top_k]:
            conf = item.get("confidence", 1.0)
            conf_marker = "~" if isinstance(conf, (int, float)) and conf < 0.5 else ""
            stale_marker = "" if not item.get("stale") else "!"
            lines.append(
                f"- {stale_marker}{conf_marker}[{item['memory_type']}] {item['content'][:200]}"
            )
        return "\n".join(lines)

    def ingest(
        self, content: str, memory_type: str = "semantic", **kwargs: Any
    ) -> dict[str, Any] | str:
        """POST /memory/ingest (full pipeline)."""
        context: dict[str, Any] = {"memory_type": memory_type}
        # Merge metadata as top-level context keys (service merges context into pipeline state)
        metadata = kwargs.get("metadata")
        if metadata and isinstance(metadata, dict):
            context.update(metadata)
        body: dict[str, Any] = {"content": content, "context": context}
        result = self._request("POST", "/memory/ingest", timeout=120, json=body)
        if err := self._fmt_error(result):
            return {"error": err}
        return result or {}

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
        """POST /memory/ingest/conversation (RLM-1g)."""
        body: dict[str, Any] = {"turns": turns}
        if session_boundaries is not None:
            body["session_boundaries"] = session_boundaries
        if conversation_id is not None:
            body["conversation_id"] = conversation_id
        if session_dates is not None:
            body["session_dates"] = session_dates
        if turns_per_chunk != 15:
            body["turns_per_chunk"] = turns_per_chunk
        if max_chunk_chars != 12000:
            body["max_chunk_chars"] = max_chunk_chars
        if max_concurrent != 4:
            body["max_concurrent"] = max_concurrent
        result = self._request(
            "POST", "/memory/ingest/conversation", timeout=300, json=body
        )
        if err := self._fmt_error(result):
            return {"error": err}
        return result or {}

    def clear_user_memories(self, **kwargs: Any) -> dict[str, Any]:
        """DELETE /memory/clear-all."""
        params: dict[str, str] = {}
        if kwargs.get("nuclear"):
            params["nuclear"] = "true"
        return self._request("DELETE", "/memory/clear-all", params=params) or {}

    def stats(self, **kwargs: Any) -> dict[str, Any]:
        """GET /memory/health."""
        return self._request("GET", "/memory/health") or {}

    def health(self) -> dict[str, Any]:
        """GET /health — API-level health check."""
        try:
            r = httpx.get(f"{self._api_url}/health", timeout=10)
            r.raise_for_status()
            return {"healthy": True, "api_url": self._api_url}
        except Exception as e:
            return {"healthy": False, "error": str(e), "api_url": self._api_url}

    def list_memories(self, **kwargs: Any) -> list[MemoryResult]:
        """GET /memory/list — list memories, optionally filtered by metadata.

        GRAPH-API-1l added `metadata_key`/`metadata_value` to this route (the
        supported replacement for the deprecated `/memory/by-metadata`). This
        method previously forwarded ONLY limit/offset while accepting arbitrary
        kwargs, so a caller passing filters got a silently UNFILTERED list —
        wrong results, no error. Forward them explicitly.
        """
        params: dict[str, str] = {}
        if "limit" in kwargs:
            params["limit"] = str(kwargs["limit"])
        if "offset" in kwargs:
            params["offset"] = str(kwargs["offset"])
        if kwargs.get("order") is not None:
            params["order"] = str(kwargs["order"])

        # The service 422s unless both are supplied together (crud.py list_memories).
        # Catch it here so the caller gets a clear message instead of an HTTP error
        # surfaced through _fmt_error.
        mkey, mval = kwargs.get("metadata_key"), kwargs.get("metadata_value")
        if (mkey is None) != (mval is None):
            missing = "metadata_value" if mkey is not None else "metadata_key"
            raise ValueError(
                f"metadata_key and metadata_value must be supplied together; {missing} is missing."
            )
        if mkey is not None:
            params["metadata_key"] = str(mkey)
            params["metadata_value"] = str(mval)

        result = self._request("GET", "/memory/list", params=params or None)
        if isinstance(result, dict):
            # Surface the error instead of masking a backend 500 as "no memories".
            if err := self._fmt_error(result):
                raise RuntimeError(err)
            # Service returns paginated dict with "items" and "total".
            # isinstance-guarded like search_by_metadata: a regressed
            # {"items": {...}} would otherwise become one blank memory per dict
            # key instead of surfacing a contract error.
            rows = result.get("items")
            raw = rows if isinstance(rows, list) else []
        else:
            raw = result if isinstance(result, list) else []
        return normalize_items(raw)

    # --- MemoryBackend protocol: NOT available in remote mode --------------------

    def ingest_structured(
        self, items: list[dict[str, Any]], **kwargs: Any
    ) -> dict[str, Any]:
        """Not available in remote mode."""
        raise NotImplementedError("Not available in remote mode. Use local backend.")

    def get_all_items_debug(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Not available in remote mode."""
        raise NotImplementedError("Not available in remote mode. Use local backend.")

    def run_evolution_cycle(self, **kwargs: Any) -> dict[str, Any]:
        """Not available in remote mode."""
        raise NotImplementedError("Not available in remote mode. Use local backend.")

    # CORE-MEMORY-DYNAMICS-1 M1b: commit_working_to_* removed — ConsolidationRouter
    # now routes at ingest.

    def run_evolver(self, evolver_name: str, **kwargs: Any) -> dict[str, Any]:
        """Not available in remote mode."""
        raise NotImplementedError("Not available in remote mode. Use local backend.")

    def run_clustering(self, **kwargs: Any) -> dict[str, Any]:
        """Not available in remote mode."""
        raise NotImplementedError("Not available in remote mode. Use local backend.")

    def reflect(self, **kwargs: Any) -> dict[str, Any]:
        """Not available in remote mode."""
        raise NotImplementedError("Not available in remote mode. Use local backend.")

    def summary(self, **kwargs: Any) -> dict[str, Any]:
        """Not available in remote mode."""
        raise NotImplementedError("Not available in remote mode. Use local backend.")

    def orphaned_notes(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Not available in remote mode."""
        raise NotImplementedError("Not available in remote mode. Use local backend.")

    def find_old_notes(self, **kwargs: Any) -> list[dict[str, Any]]:
        """Not available in remote mode."""
        raise NotImplementedError("Not available in remote mode. Use local backend.")

    def personalize(self, query: str, **kwargs: Any) -> dict[str, Any]:
        """Not available in remote mode."""
        raise NotImplementedError("Not available in remote mode. Use local backend.")

    def update_from_feedback(
        self, item_id: str, feedback: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Not available in remote mode."""
        raise NotImplementedError("Not available in remote mode. Use local backend.")

    def ground(self, item_id: str, **kwargs: Any) -> dict[str, Any]:
        """Not available in remote mode."""
        raise NotImplementedError("Not available in remote mode. Use local backend.")

    def link(self, source_id: str, target_id: str, **kwargs: Any) -> dict[str, Any]:
        """Not available in remote mode."""
        raise NotImplementedError("Not available in remote mode. Use local backend.")

    def add_edge(
        self, source_id: str, target_id: str, relation: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Not available in remote mode."""
        raise NotImplementedError("Not available in remote mode. Use local backend.")

    def get_links(self, item_id: str, **kwargs: Any) -> list[dict[str, Any]]:
        """Not available in remote mode."""
        raise NotImplementedError("Not available in remote mode. Use local backend.")

    def get_neighbors(self, item_id: str, **kwargs: Any) -> dict[str, Any]:
        """Not available in remote mode."""
        raise NotImplementedError("Not available in remote mode. Use local backend.")

    def read_around(
        self,
        item_id: str,
        char_budget: int = 20000,
        before_ratio: float = 0.3,
        after_ratio: float = 0.7,
        cursor: dict | None = None,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """POST /memory/read-around — auto-centered conversation read (CORE-RECALL-CENTERED-1 P2)."""
        body: dict[str, Any] = {
            "item_id": item_id,
            "char_budget": char_budget,
            "before_ratio": before_ratio,
            "after_ratio": after_ratio,
        }
        if cursor is not None:
            body["cursor"] = cursor
        return self._request("POST", "/memory/read-around", json=body)

    def find_shortest_path(
        self, source_id: str, target_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        """Not available in remote mode."""
        raise NotImplementedError("Not available in remote mode. Use local backend.")

    # --- Retrieval feedback (SELF-IMPROVE-6) -------------------------------------

    def submit_feedback(
        self, search_session_id: str, result_used: list[str], **kwargs: Any
    ) -> dict[str, Any]:
        """POST /memory/result-feedback — submit result-selection feedback."""
        return self._request(
            "POST",
            "/memory/result-feedback",
            json={"search_session_id": search_session_id, "result_used": result_used},
        )
