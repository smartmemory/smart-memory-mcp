"""OAuth state storage wrappers for the hosted server (S4).

FastMCP writes three kinds of record through `client_storage`: DCR client
registrations (no TTL at all), OAuth transactions (a short explicit TTL), and
upstream token state (the lifetime the upstream reported). Only the first kind
needs a default, and the third must never be shortened.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, SupportsFloat

from key_value.aio.protocols.key_value import AsyncKeyValue
from key_value.aio.wrappers.base import BaseWrapper

# DCR client registrations arrive with no TTL and would otherwise live forever
# in a Redis shared with nothing else to evict them. Claude and ChatGPT
# re-register on reconnect, so 30 days costs a client nothing.
DEFAULT_CLIENT_RECORD_TTL = 30 * 86400


class DefaultTTLWrapper(BaseWrapper):
    """Substitute `default_ttl` for writes that carry no TTL. Never clamp.

    Deliberately NOT `TTLClampWrapper`: a max_ttl would truncate the stored
    upstream refresh state while FastMCP goes on issuing a refresh JWT for the
    original lifetime (`oauth_proxy/proxy.py:1381-1400,1466-1487`), orphaning a
    token the server itself still calls valid (round 4 must-fix 1, round 5
    must-fix 1). Explicit TTLs pass through in both directions, untouched.
    """

    def __init__(
        self,
        key_value: AsyncKeyValue,
        default_ttl: SupportsFloat = DEFAULT_CLIENT_RECORD_TTL,
    ) -> None:
        self.key_value: AsyncKeyValue = key_value
        self.default_ttl: float = float(default_ttl)
        super().__init__()

    def _ttl(self, ttl: SupportsFloat | None) -> float:
        return self.default_ttl if ttl is None else float(ttl)

    async def put(
        self,
        key: str,
        value: Mapping[str, Any],
        *,
        collection: str | None = None,
        ttl: SupportsFloat | None = None,
    ) -> None:
        await self.key_value.put(
            collection=collection, key=key, value=value, ttl=self._ttl(ttl)
        )

    async def put_many(
        self,
        keys: Sequence[str],
        values: Sequence[Mapping[str, Any]],
        *,
        collection: str | None = None,
        ttl: SupportsFloat | None = None,
    ) -> None:
        await self.key_value.put_many(
            keys=keys, values=values, collection=collection, ttl=self._ttl(ttl)
        )
