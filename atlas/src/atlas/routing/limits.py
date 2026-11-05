"""Request rate limits for the public route preview service.

Token buckets, all in memory, with fixed bounds:

- a global bucket per endpoint class that no request header can influence, so spoofed or
  rotating client addresses still cannot push the service past a fixed request rate;
- a per-client bucket per class, keyed on the client address. Only one configured header
  is trusted for that address (Railway's edge sets `X-Real-IP`). `X-Forwarded-For` is never
  read. A missing, repeated or malformed header falls back to one shared bucket.

At most `max_clients` client entries are kept, least recently seen first out, so memory stays
bounded whatever the traffic. Keys are a salted hash, never the raw address, and nothing here
is logged. IPv6 clients are grouped by /64, since one subscriber usually holds a whole /64.
"""
from __future__ import annotations

import hashlib
import ipaddress
import math
import os
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Callable

ROUTE = 'route'
READ = 'read'


@dataclass(frozen=True)
class Rate:
    burst: float
    per_s: float


@dataclass(frozen=True)
class Limits:
    client: dict[str, Rate] = field(default_factory=lambda: {
        ROUTE: Rate(burst=10, per_s=1 / 3),    # 20 route searches a minute after a burst of 10
        READ: Rate(burst=30, per_s=1.0),
    })
    total: dict[str, Rate] = field(default_factory=lambda: {
        ROUTE: Rate(burst=30, per_s=4.0),
        READ: Rate(burst=100, per_s=30.0),
    })
    max_clients: int = 4096
    client_ip_header: str | None = None   # lower-case trusted header; None shares one bucket


def endpoint_class(method: str, path: str) -> str:
    return ROUTE if path == '/v1/route-preview' else READ


class Bucket:
    __slots__ = ('tokens', 'stamp')

    def __init__(self, burst: float, now: float):
        self.tokens, self.stamp = burst, now

    def take(self, rate: Rate, now: float) -> float:
        """Take one token. Returns 0 on success, or the seconds until one is available."""
        self.tokens = min(rate.burst, self.tokens + (now - self.stamp) * rate.per_s)
        self.stamp = now
        if self.tokens >= 1:
            self.tokens -= 1
            return 0.0
        return (1 - self.tokens) / rate.per_s


class RateLimiter:
    def __init__(self, limits: Limits = Limits(), clock: Callable[[], float] = time.monotonic):
        self.limits, self.clock = limits, clock
        self._salt = os.urandom(16)
        self._lock = threading.Lock()
        now = clock()
        self._total = {k: Bucket(r.burst, now) for k, r in limits.total.items()}
        self._clients: OrderedDict[tuple[str, bytes], Bucket] = OrderedDict()

    def client_key(self, scope: dict) -> bytes:
        return hashlib.blake2b(client_address(scope, self.limits.client_ip_header).encode(),
                               key=self._salt, digest_size=16).digest()

    def check(self, kind: str, key: bytes) -> float:
        """0 when allowed, else seconds to wait. The client bucket is charged first, so one
        client hammering the service cannot use up the global budget for everyone else."""
        now = self.clock()
        with self._lock:
            ck = (kind, key)
            bucket = self._clients.get(ck)
            if bucket is None:
                bucket = self._clients[ck] = Bucket(self.limits.client[kind].burst, now)
                while len(self._clients) > self.limits.max_clients:
                    self._clients.popitem(last=False)
            else:
                self._clients.move_to_end(ck)
            wait = bucket.take(self.limits.client[kind], now)
            if wait:
                return wait
            wait = self._total[kind].take(self.limits.total[kind], now)
            if wait:
                bucket.tokens += 1   # refund: the request was not served
            return wait

    @property
    def tracked_clients(self) -> int:
        return len(self._clients)


def client_address(scope: dict, header: str | None) -> str:
    """The client address to limit on, normalised. Never logged."""
    raw = None
    if header:
        values = [v for k, v in scope.get('headers', []) if k.decode('latin-1').lower() == header]
        if len(values) != 1:
            return 'untrusted'
        raw = values[0].decode('latin-1').strip()
    else:
        return 'shared'
    if not raw:
        return 'untrusted'
    try:
        ip = ipaddress.ip_address(raw)
    except ValueError:
        return 'untrusted'
    if isinstance(ip, ipaddress.IPv6Address):
        if ip.ipv4_mapped:
            return str(ip.ipv4_mapped)
        return str(ipaddress.ip_network(f'{ip}/64', strict=False))
    return str(ip)


class RateLimit:
    """ASGI middleware. A limited request gets `429 rate_limited` with Retry-After and never
    reaches the endpoint, so it costs no route search and no request body read."""

    def __init__(self, app, limiter: RateLimiter, reject: Callable[[int], object]):
        self.app, self.limiter, self.reject = app, limiter, reject

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            await self.app(scope, receive, send)
            return
        kind = endpoint_class(scope.get('method', ''), scope.get('path', ''))
        wait = self.limiter.check(kind, self.limiter.client_key(scope))
        if wait:
            response = self.reject(max(1, math.ceil(wait)))
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)
