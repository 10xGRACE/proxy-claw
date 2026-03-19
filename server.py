#!/usr/bin/env python3
"""Claude Code Proxy — entry point."""

import argparse
import asyncio
import json
import logging
from pathlib import Path

import aiohttp
from aiohttp import web

from core import ProxyHandler, TokenManager
from middleware import auth_middleware, body_limit_middleware, rate_limit_middleware, security_middleware
from stats import StatsCollector

DASHBOARD_HTML = Path(__file__).parent / "dashboard.html"

log = logging.getLogger("proxy")


async def handle_dashboard(_: web.Request) -> web.Response:
    return web.Response(text=DASHBOARD_HTML.read_text(), content_type="text/html")


async def handle_health(request: web.Request) -> web.Response:
    token_mgr: TokenManager = request.app["token_mgr"]
    stats: StatsCollector = request.app["stats"]
    exp = token_mgr.expires_at()
    import time
    return web.json_response({
        "status": "ok" if time.time() * 1000 < exp else "token_expired",
        "uptime": round(time.time() - stats.start_time),
    })


async def handle_stats(request: web.Request) -> web.Response:
    stats: StatsCollector = request.app["stats"]
    token_mgr: TokenManager = request.app["token_mgr"]
    return web.json_response(stats.snapshot(token_mgr.expires_at()))


async def handle_reset(request: web.Request) -> web.Response:
    stats: StatsCollector = request.app["stats"]
    token_mgr: TokenManager = request.app["token_mgr"]
    stats.reset()
    await stats.broadcast("stats", stats.snapshot(token_mgr.expires_at()))
    return web.json_response({"status": "ok"})


async def handle_events(request: web.Request) -> web.StreamResponse:
    stats: StatsCollector = request.app["stats"]
    token_mgr: TokenManager = request.app["token_mgr"]

    resp = web.StreamResponse(headers={
        "Content-Type": "text/event-stream",
        "Cache-Control": "no-cache",
        "X-Accel-Buffering": "no",
    })
    await resp.prepare(request)

    q = stats.add_sse_client()

    # Send initial snapshot
    snap = json.dumps(stats.snapshot(token_mgr.expires_at()))
    await resp.write(f"event: stats\ndata: {snap}\n\n".encode())

    try:
        while True:
            msg = await q.get()
            await resp.write(msg.encode())
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    finally:
        stats.remove_sse_client(q)
    return resp


async def on_startup(app: web.Application):
    connector = aiohttp.TCPConnector(
        limit=50,
        limit_per_host=20,
        ttl_dns_cache=300,
        enable_cleanup_closed=True,
    )
    session = aiohttp.ClientSession(connector=connector, auto_decompress=False)
    app["session"] = session

    stats: StatsCollector = app["stats"]
    token_mgr = TokenManager(session, stats)
    proxy = ProxyHandler(session, token_mgr, stats)

    app["token_mgr"] = token_mgr
    app["proxy"] = proxy

    # Register the catch-all proxy route now that handler exists
    app.router.add_route("*", "/{path:.*}", proxy.handle)
    log.info("session and token manager ready")


async def on_shutdown(app: web.Application):
    log.info("shutting down — draining active streams…")
    session: aiohttp.ClientSession = app["session"]
    await session.close()


def build_app(port: int, secret: str | None, max_body: int) -> web.Application:
    stats = StatsCollector()

    app = web.Application(middlewares=[
        security_middleware,
        body_limit_middleware(max_body),
        rate_limit_middleware(rate=2.0, burst=10),
        auth_middleware(secret),
    ])

    app["stats"] = stats

    app.router.add_get("/dashboard", handle_dashboard)
    app.router.add_get("/dashboard/stats", handle_stats)
    app.router.add_get("/dashboard/events", handle_events)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/dashboard/reset", handle_reset)

    app.on_startup.append(on_startup)
    app.on_shutdown.append(on_shutdown)

    return app


def main():
    parser = argparse.ArgumentParser(description="Claude Code Proxy")
    parser.add_argument("--port", type=int, default=9191, help="listen port (default: 9191)")
    parser.add_argument("--secret", default=None, help="shared secret for proxy auth")
    parser.add_argument("--max-body", type=int, default=10 * 1024 * 1024,
                        help="max request body in bytes (default: 10MB)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    log.info("starting on port %d", args.port)
    if args.secret:
        log.info("auth enabled (secret required)")
    else:
        log.info("auth disabled (no --secret)")

    app = build_app(args.port, args.secret, args.max_body)
    web.run_app(app, host="0.0.0.0", port=args.port, shutdown_timeout=60.0,
                print=lambda _: None)  # suppress aiohttp's own startup message


if __name__ == "__main__":
    main()
