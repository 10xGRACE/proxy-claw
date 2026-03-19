"""Middleware chain: security headers, rate limiting, auth check."""

import asyncio
import time

from aiohttp import web

# Paths that bypass auth and rate limiting
OPEN_PREFIXES = ("/dashboard", "/health")


def is_open_path(path: str) -> bool:
    return any(path.startswith(p) for p in OPEN_PREFIXES)


# ── Security headers ────────────────────────────────────────


@web.middleware
async def security_middleware(request: web.Request, handler):
    response = await handler(request)
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    return response


# ── Request body size cap ───────────────────────────────────


def body_limit_middleware(max_bytes: int):
    @web.middleware
    async def mw(request: web.Request, handler):
        if is_open_path(request.path):
            return await handler(request)

        cl = request.headers.get("Content-Length")
        if cl and int(cl) > max_bytes:
            return web.json_response(
                {"error": f"payload too large (max {max_bytes // 1024 // 1024}MB)"},
                status=413,
            )
        return await handler(request)

    return mw


# ── Token bucket rate limiter ───────────────────────────────


class _Bucket:
    __slots__ = ("tokens", "last", "rate", "capacity")

    def __init__(self, rate: float, capacity: int):
        self.rate = rate
        self.capacity = capacity
        self.tokens = float(capacity)
        self.last = time.monotonic()

    def try_acquire(self) -> bool:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.last) * self.rate)
        self.last = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False


def rate_limit_middleware(rate: float = 2.0, burst: int = 10):
    """Per-client token bucket. `rate` = tokens/sec, `burst` = max bucket size."""
    buckets: dict[str, _Bucket] = {}

    @web.middleware
    async def mw(request: web.Request, handler):
        if is_open_path(request.path):
            return await handler(request)

        ip = request.remote or "unknown"
        if ip not in buckets:
            buckets[ip] = _Bucket(rate, burst)

        if not buckets[ip].try_acquire():
            return web.json_response(
                {"error": "rate limit exceeded"},
                status=429,
                headers={"Retry-After": "1"},
            )
        return await handler(request)

    return mw


# ── Auth (shared secret) ───────────────────────────────────


def auth_middleware(secret: str | None):
    @web.middleware
    async def mw(request: web.Request, handler):
        if secret is None or is_open_path(request.path):
            return await handler(request)

        key = request.headers.get("x-api-key", "")
        bearer = request.headers.get("authorization", "")

        if key == secret or bearer == f"Bearer {secret}":
            return await handler(request)

        return web.json_response({"error": "unauthorized"}, status=401)

    return mw
