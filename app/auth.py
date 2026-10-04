"""Token helpers and in-memory rate limiting.

Token hashing lives in `store.hash_token`; this module owns request-scoped
concerns: extracting the bearer token from a request or WebSocket, resolving it
to a device, and throttling abusive callers.
"""

from __future__ import annotations

import time
from collections import deque

from fastapi import Request, WebSocket

from .config import Settings
from .store import Device, Store

BEARER_PREFIX = "bearer "


class SlidingWindowLimiter:
    """Fixed-capacity sliding window counter, keyed by an arbitrary string."""

    def __init__(self, limit: int, window_seconds: float) -> None:
        self.limit = max(1, limit)
        self.window = float(window_seconds)
        self._hits: dict[str, deque[float]] = {}

    def check(self, key: str, now: float | None = None) -> tuple[bool, int]:
        """Return `(allowed, retry_after_seconds)`."""
        moment = time.monotonic() if now is None else now
        bucket = self._hits.setdefault(key, deque())
        cutoff = moment - self.window
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()
        if len(bucket) >= self.limit:
            retry_after = max(1, int(self.window - (moment - bucket[0])) + 1)
            return False, retry_after
        bucket.append(moment)
        return True, 0

    def reset(self, key: str) -> None:
        self._hits.pop(key, None)

    def clear(self) -> None:
        self._hits.clear()


def client_key(request: Request | WebSocket, settings: Settings) -> str:
    """Best-effort client identity for rate limiting.

    Forwarded headers are only honoured when the deployment explicitly opts in
    via `DSH_RELAY_TRUST_PROXY_HEADERS`, because a spoofed `X-Forwarded-For`
    would otherwise defeat the limiter.
    """
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    client = request.client
    return client.host if client and client.host else "unknown"


def bearer_token(request: Request | WebSocket) -> str:
    """Extract a token from `Authorization: Bearer` or the `token` query param.

    The query parameter exists because the WHATWG `WebSocket` constructor used
    by the desktop plugin cannot set request headers.
    """
    header = request.headers.get("authorization") or ""
    if header.lower().startswith(BEARER_PREFIX):
        candidate = header[len(BEARER_PREFIX) :].strip()
        if candidate:
            return candidate
    return request.query_params.get("token", "") or ""


def resolve_device(store: Store, token: str) -> Device | None:
    return store.verify_device(token)
