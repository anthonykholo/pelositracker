"""Process-wide HTTPX client pool, owned by the app lifespan.

Event setup and discovery repeatedly call the same handful of hosts
(gamma-api / clob.polymarket.com, api.the-odds-api.com). Routing those calls
through one keep-alive pool reuses TCP/TLS connections instead of handshaking
anew on every event add and every discovery tick.

The pool is opened on startup and closed on shutdown by ``lifespan`` -- it is
not an unmanaged global. When no pool is open (unit tests, ad-hoc calls),
``current_shared_client`` returns ``None`` and callers fall back to a
short-lived client, so behavior is unchanged outside the running app.

The long-lived poll loops (odds/pinnacle/action-network) already hold one
client for their whole lifetime and are intentionally left alone; this pool is
only for the otherwise-per-call one-shot fetches.
"""
from __future__ import annotations

import httpx

# Bound the pool so a burst of concurrent discovery/setup calls can't open an
# unbounded number of sockets to the shared hosts.
_LIMITS = httpx.Limits(max_keepalive_connections=20, max_connections=100)
_DEFAULT_TIMEOUT = 15.0

_shared: httpx.AsyncClient | None = None


def open_shared_client() -> httpx.AsyncClient:
    """Open the shared pool if needed and return it. Idempotent; call on startup."""
    global _shared
    if _shared is None or _shared.is_closed:
        _shared = httpx.AsyncClient(timeout=_DEFAULT_TIMEOUT, limits=_LIMITS)
    return _shared


def current_shared_client() -> httpx.AsyncClient | None:
    """Return the open shared pool, or ``None`` when nothing is open."""
    if _shared is not None and not _shared.is_closed:
        return _shared
    return None


async def close_shared_client() -> None:
    """Close the shared pool. Call on shutdown, after in-flight callers finish."""
    global _shared
    client, _shared = _shared, None
    if client is not None and not client.is_closed:
        await client.aclose()
