"""
Authorized cache for private, per-identity results.

The rule this module exists to enforce: authorization is re-checked on EVERY
call — including a cache hit. A value cached while an identity was authorized
must never be served once that authorization is gone, just because it is
still sitting warm in memory. This is the concrete mechanism behind "private
operations require authorization before cache hits" from the project's
privacy rules — nothing in this codebase caches a private result without
going through here.

Process-local, in-memory, TTL-based. Not a distributed cache and not meant to
be one yet — hosted multi-user mode is still blocked, so a single process is
the whole deployment.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable, TypeVar

from portfolio_agent.services.account_service import resolve_portfolio
from portfolio_agent.services.context import RequestContext

T = TypeVar("T")


@dataclass
class _Entry:
    value: object
    expires_at: float


class AuthorizedCache:
    """
    A tiny cache keyed by (verified identity, resource key). get_or_compute
    calls resolve_portfolio(ctx) before consulting the cache OR computing a
    fresh value — a failed authorization check raises before either happens,
    so a warm entry from a now-unauthorized identity is never returned.
    """

    def __init__(self, ttl_seconds: float = 300.0):
        self._ttl = ttl_seconds
        self._store: dict[tuple[str, str], _Entry] = {}

    def get_or_compute(self, ctx: RequestContext, key: str, compute: Callable[[], T]) -> T:
        resolve_portfolio(ctx)   # authorization FIRST, on every call, cache hit or not
        cache_key = (ctx.actor, key)
        now = time.monotonic()
        entry = self._store.get(cache_key)
        if entry is not None and entry.expires_at > now:
            return entry.value
        value = compute()
        self._store[cache_key] = _Entry(value=value, expires_at=now + self._ttl)
        return value

    def invalidate(self, ctx: RequestContext, key: str) -> None:
        self._store.pop((ctx.actor, key), None)

    def invalidate_all(self) -> None:
        self._store.clear()
