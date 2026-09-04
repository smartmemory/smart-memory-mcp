"""HostedRemoteBackend — RemoteBackend with 401 re-exchange semantics (S1).

`RemoteBackend._request` turns a svc-api 401 into an error dict and `search`
turns it into a RuntimeError, so neither could implement the contract's
"one re-exchange on 401". Both now call `_on_unauthorized`; this subclass
overrides that single hook (round 2 #10, round 3 M6).
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from ..backends.remote import RemoteBackend
from .exchange import ExchangeCache
from .identity import HostedAuthError

logger = logging.getLogger(__name__)


class HostedNdaRequiredError(RuntimeError):
    """svc-api has gated this hosted call on accepting the beta agreement."""


class HostedRemoteBackend(RemoteBackend):
    """Per-call backend for one hosted user in one workspace."""

    def __init__(
        self,
        api_url: str,
        api_key: str,
        team_id: str,
        *,
        fingerprint: str,
        cache: ExchangeCache,
    ) -> None:
        super().__init__(api_url=api_url, api_key=api_key, team_id=team_id)
        self._fingerprint = fingerprint
        self._cache = cache

    def _on_unauthorized(self, response: httpx.Response) -> None:
        """Drop the cached exchange for this token, then fail loudly.

        The client's NEXT call performs a fresh exchange — the one retry the
        contract promises, executed by the client, not by a hidden loop here.
        """
        logger.warning(
            "svc-api rejected the hosted session (401); invalidating the cached "
            "exchange for fingerprint %s.",
            self._fingerprint[:12],
        )
        self._cache.invalidate(self._fingerprint)
        raise HostedAuthError("session expired, retry")

    def _search_request_body(self, body: dict[str, Any]) -> dict[str, Any]:
        """Require the service to drop speculative-derived search results."""
        body["exclude_speculative"] = True
        return body

    def _request(
        self,
        method: str,
        path: str,
        workspace_id: str | None = None,
        timeout: int = 30,
        **kwargs: Any,
    ) -> Any:
        """Add the hosted origin policy to direct search-route calls too."""
        if path == "/memory/search":
            if method.upper() == "GET":
                params = dict(kwargs.get("params") or {})
                params["exclude_speculative"] = "true"
                kwargs["params"] = params
            elif method.upper() == "POST":
                kwargs["json"] = self._search_request_body(
                    dict(kwargs.get("json") or {})
                )
        return super()._request(method, path, workspace_id, timeout, **kwargs)

    def _on_http_error(self, response: httpx.Response) -> None:
        """Promote the beta gate to one shared hosted tool-error path."""
        if response.status_code != 403:
            return
        try:
            detail = response.json().get("detail")
        except (TypeError, ValueError):
            return
        if isinstance(detail, dict) and detail.get("code") == "nda_required":
            raise HostedNdaRequiredError
