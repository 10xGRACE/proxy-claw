# Claude Code Proxy — Architecture Plan

## Overview

A reverse proxy that routes Claude Code CLI requests from multiple devices through a single authenticated device. Includes a live monitoring dashboard. Designed for robustness: streaming backpressure, token auto-refresh, rate limiting, circuit breaking, and graceful shutdown.

---

## System Architecture

```
┌─────────────────────────────────────────────────────────────────────────────┐
│                           TAILSCALE NETWORK                                │
│                                                                             │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐                      │
│  │  Device A    │  │  Device B    │  │  Device C    │                      │
│  │  Claude Code │  │  Claude Code │  │  Claude Code │                      │
│  │  BASE_URL=   │  │  BASE_URL=   │  │  BASE_URL=   │                      │
│  │  http://ts:  │  │  http://ts:  │  │  http://ts:  │                      │
│  │  9191        │  │  9191        │  │  9191        │                      │
│  └──────┬───────┘  └──────┬───────┘  └──────┬───────┘                      │
│         │                 │                 │                               │
│         └────────────────┐│┌────────────────┘                               │
│                          │││                                                │
│                          ▼▼▼                                                │
│  ┌──────────────────────────────────────────────────────────────────────┐   │
│  │                     PROXY DEVICE (0.0.0.0:9191)                     │   │
│  │                                                                      │   │
│  │  ┌────────────────────────────────────────────────────────────────┐  │   │
│  │  │                        MIDDLEWARE CHAIN                        │  │   │
│  │  │                                                                │  │   │
│  │  │   ┌──────────┐   ┌──────────────┐   ┌────────────────────┐   │  │   │
│  │  │   │ Security │──>│  Rate Limit  │──>│  Auth (secret key) │   │  │   │
│  │  │   │ Headers  │   │  per-client  │   │  x-api-key check   │   │  │   │
│  │  │   │ Size Cap │   │  token bucket│   │                    │   │  │   │
│  │  │   └──────────┘   └──────────────┘   └────────────────────┘   │  │   │
│  │  └────────────────────────────┬───────────────────────────────────┘  │   │
│  │                               │                                      │   │
│  │                               ▼                                      │   │
│  │  ┌─────────────────────────────────────────────────────────────────┐ │   │
│  │  │                         ROUTER                                  │ │   │
│  │  │                                                                 │ │   │
│  │  │   /v1/*  ──────────────>  ProxyHandler                         │ │   │
│  │  │   /dashboard  ─────────>  DashboardHandler (serves HTML)       │ │   │
│  │  │   /dashboard/events  ──>  SSEHandler (live stats stream)       │ │   │
│  │  │   /dashboard/stats  ───>  StatsHandler (JSON snapshot)         │ │   │
│  │  │   /health  ────────────>  HealthHandler                        │ │   │
│  │  └─────────────────────────────────────────────────────────────────┘ │   │
│  │                               │                                      │   │
│  │                               ▼                                      │   │
│  │  ┌─────────────────────────────────────────────────────────────────┐ │   │
│  │  │                      PROXY HANDLER                              │ │   │
│  │  │                                                                 │ │   │
│  │  │  1. Strip incoming auth headers (x-api-key, Authorization)     │ │   │
│  │  │  2. Get valid token from TokenManager                          │ │   │
│  │  │  3. Inject  Authorization: Bearer <oauth-token>                │ │   │
│  │  │  4. Forward request to api.anthropic.com                       │ │   │
│  │  │  5. Stream response chunks with backpressure (drain())         │ │   │
│  │  │  6. Record metrics in StatsCollector                           │ │   │
│  │  │  7. Broadcast event to dashboard SSE clients                   │ │   │
│  │  │                                                                 │ │   │
│  │  │  Error handling:                                                │ │   │
│  │  │  - ClientConnectorError  → 502 Bad Gateway                     │ │   │
│  │  │  - TimeoutError          → 504 Gateway Timeout                 │ │   │
│  │  │  - ClientPayloadError    → 502 (upstream closed mid-stream)    │ │   │
│  │  │  - 429 from upstream     → pass through + circuit breaker      │ │   │
│  │  │  - 401 from upstream     → trigger token refresh + retry once  │ │   │
│  │  └─────────────────────────────────────────────────────────────────┘ │   │
│  │                               │                                      │   │
│  │                               ▼                                      │   │
│  │  ┌─────────────────────────────────────────────────────────────────┐ │   │
│  │  │                     TOKEN MANAGER                               │ │   │
│  │  │                                                                 │ │   │
│  │  │  Credentials: ~/.claude/.credentials.json                      │ │   │
│  │  │                                                                 │ │   │
│  │  │  ┌─────────────────────────────────────────────────────────┐   │ │   │
│  │  │  │  accessToken   sk-ant-oat01-...                        │   │ │   │
│  │  │  │  refreshToken  sk-ant-ort01-...                        │   │ │   │
│  │  │  │  expiresAt     <unix ms>                               │   │ │   │
│  │  │  └─────────────────────────────────────────────────────────┘   │ │   │
│  │  │                                                                 │ │   │
│  │  │  • Watches file mtime for external changes                     │ │   │
│  │  │  • Pre-emptive refresh 60s before expiry                       │ │   │
│  │  │  • Mutex lock prevents concurrent refresh races                │ │   │
│  │  │  • Refresh endpoint: platform.claude.com/v1/oauth/token        │ │   │
│  │  │  • Client ID: claude-code-20250219                             │ │   │
│  │  │  • Writes refreshed creds back to file                        │ │   │
│  │  └─────────────────────────────────────────────────────────────────┘ │   │
│  │                               │                                      │   │
│  │                               ▼                                      │   │
│  │  ┌─────────────────────────────────────────────────────────────────┐ │   │
│  │  │                    STATS COLLECTOR                              │ │   │
│  │  │                                                                 │ │   │
│  │  │  Tracked:                                                       │ │   │
│  │  │  • total / ok / fail request counts                            │ │   │
│  │  │  • active stream count                                         │ │   │
│  │  │  • bytes up / down                                             │ │   │
│  │  │  • per-client: reqs, bytes, first_seen, last_seen              │ │   │
│  │  │  • request log (ring buffer, last 200)                         │ │   │
│  │  │  • token refresh count & last refresh time                     │ │   │
│  │  │  • uptime                                                       │ │   │
│  │  │                                                                 │ │   │
│  │  │  Broadcast:                                                     │ │   │
│  │  │  • SSE "request" event on each completed proxy request         │ │   │
│  │  │  • SSE "stats" event on stats changes                          │ │   │
│  │  │  • Manages list of SSE client queues (auto-prune dead ones)    │ │   │
│  │  └─────────────────────────────────────────────────────────────────┘ │   │
│  └──────────────────────────────────────────────────────────────────────┘   │
│                               │                                             │
└───────────────────────────────│─────────────────────────────────────────────┘
                                │
                                ▼
                   ┌─────────────────────────┐
                   │   api.anthropic.com     │
                   │                         │
                   │   POST /v1/messages     │
                   │   (streaming SSE)       │
                   └─────────────────────────┘
```

---

## Frontend Architecture (dashboard.html)

```
┌─────────────────────────────────────────────────────────────────────┐
│  dashboard.html — Single Page, No Build Step, Vanilla JS           │
│                                                                     │
│  Data Flow:                                                         │
│  EventSource(/dashboard/events) ──> SSE messages ──> DOM updates   │
│  fetch(/dashboard/stats)        ──> initial load   ──> DOM render  │
│                                                                     │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  HEADER BAR                                                   │  │
│  │  ┌──────────────────────────┐  ┌──────────────────────────┐  │  │
│  │  │  Claude Code Proxy       │  │  ● Online  / ● Offline  │  │  │
│  │  │  (title)                 │  │  (connection indicator)  │  │  │
│  │  └──────────────────────────┘  └──────────────────────────┘  │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  STATS CARDS (grid, 5 columns)                                │  │
│  │                                                               │  │
│  │  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌────────┐ ┌──────┐ │  │
│  │  │ Uptime   │ │ Requests │ │ Active   │ │ Errors │ │Token │ │  │
│  │  │          │ │          │ │ Streams  │ │  Rate  │ │Status│ │  │
│  │  │ 2h 14m   │ │ 347      │ │ 2        │ │ 0.3%   │ │Valid │ │  │
│  │  │          │ │ 12.4 MB  │ │          │ │ 1 fail │ │ 2h   │ │  │
│  │  └──────────┘ └──────────┘ └──────────┘ └────────┘ └──────┘ │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  REQUEST LOG (live table, scrollable, last 50 entries)        │  │
│  │                                                               │  │
│  │  Time     Method  Path          Client      Status  Duration  │  │
│  │  ──────── ─────── ───────────── ─────────── ─────── ──────── │  │
│  │  14:32:01 POST    /v1/messages  100.64.0.3  200     4.2s     │  │
│  │  14:31:45 POST    /v1/messages  100.64.0.5  200     12.8s    │  │
│  │  14:31:02 POST    /v1/messages  100.64.0.3  200     3.1s     │  │
│  │  14:30:58 POST    /v1/messages  100.64.0.3  429     0.2s     │  │
│  │  ...                                                          │  │
│  │                                                               │  │
│  │  Color coding:                                                │  │
│  │  • 2xx = green   • 4xx = amber   • 5xx = red                 │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  ┌───────────────────────────────────────────────────────────────┐  │
│  │  CLIENTS TABLE                                                │  │
│  │                                                               │  │
│  │  IP Address     Requests   Data       First Seen   Last Seen  │  │
│  │  ────────────── ────────── ────────── ──────────── ──────────│  │
│  │  100.64.0.3     142        8.2 MB     2h ago       12s ago   │  │
│  │  100.64.0.5     89         4.1 MB     1h ago       3m ago    │  │
│  └───────────────────────────────────────────────────────────────┘  │
│                                                                     │
│  JS Modules (inline):                                               │
│  • SSEManager    — EventSource connection + auto-reconnect         │
│  • StatsRenderer — updates stat cards from SSE "stats" events      │
│  • LogRenderer   — prepends rows to request log from "request"     │
│  • TimeUtils     — relative time formatting, uptime display        │
│  • ByteUtils     — human-readable byte formatting                  │
└─────────────────────────────────────────────────────────────────────┘
```

---

## File Map

```
proxy/
├── server.py              Main entry point — CLI args, app setup, route registration, run loop
├── core.py                TokenManager + ProxyHandler (the two core concerns)
├── middleware.py           Auth check, per-client rate limiter, request size cap, security headers
├── stats.py               StatsCollector + SSE broadcaster
├── dashboard.html          Frontend SPA (single file, inline CSS/JS, no build step)
├── requirements.txt        Python dependencies
├── proxy.service           Systemd user service file (auto-start on boot)
└── plan.md                 This file
```

### File Details

#### `server.py` — Entry Point
```
Responsibilities:
  • Parse CLI args: --port (default 9191), --secret, --max-body (default 10MB)
  • Create aiohttp.Application
  • Initialize shared aiohttp.ClientSession with connection pooling:
      - TCPConnector(limit=50, limit_per_host=20, ttl_dns_cache=300, enable_cleanup_closed=True)
  • Wire middleware chain: security → rate_limit → auth
  • Register routes:
      GET  /dashboard          → serve dashboard.html
      GET  /dashboard/stats    → JSON stats snapshot
      GET  /dashboard/events   → SSE event stream
      GET  /health             → health check (token validity + uptime)
      *    /{path:.*}          → proxy handler (catch-all)
  • Graceful shutdown: on_shutdown hook drains active streams (60s timeout)
  • Logging setup: structured, timestamps

Dependencies: core.py, middleware.py, stats.py
```

#### `core.py` — Token Manager + Proxy Handler
```
class TokenManager:
  Responsibilities:
    • Load credentials from ~/.claude/.credentials.json
    • Watch file mtime — reload on external changes (Claude Code refreshes it too)
    • Pre-emptive refresh: if token expires within 60s, refresh before using
    • Refresh flow:
        POST https://platform.claude.com/v1/oauth/token
        Body: grant_type=refresh_token & refresh_token=<token> & client_id=claude-code-20250219
    • Mutex (asyncio.Lock) prevents concurrent refresh from parallel requests
    • Writes refreshed credentials back to file (so Claude Code on same device stays in sync)
    • On 401 from upstream: force-refresh token + retry request once

class ProxyHandler:
  Responsibilities:
    • Strip incoming auth headers (x-api-key, Authorization) + Accept-Encoding
    • Get valid token from TokenManager
    • Inject Authorization: Bearer <token> + Host: api.anthropic.com
    • Ensure anthropic-beta includes: claude-code-20250219,oauth-2025-04-20
    • Inject billing system message into JSON body if missing
    • Filter hop-by-hop headers (connection, transfer-encoding, etc.)
    • Forward request to https://api.anthropic.com{path}
    • Stream response with backpressure:
        async for chunk in upstream.content.iter_any():
            await response.write(chunk)
            await response.drain()
    • On client disconnect: upstream connection closed via finally block
    • On upstream disconnect mid-stream: log, close response gracefully
    • Record metrics in StatsCollector after each request
    • Broadcast SSE event to dashboard

  Error handling:
    • aiohttp.ClientConnectorError      → 502
    • asyncio.TimeoutError              → 504
    • aiohttp.ClientPayloadError        → 502 (upstream closed mid-stream)
    • aiohttp.ServerDisconnectedError   → 502
    • 401 from upstream                 → refresh token, retry once
    • 429 from upstream                 → pass through to client

  Timeouts:
    • total=600s (10 min max for any request including streaming)
    • sock_read=300s (5 min read timeout for slow streams)
```

#### `middleware.py` — Security Layer
```
auth_middleware:
  • If --secret configured: validate x-api-key header or Authorization: Bearer <secret>
  • Skip auth for /dashboard/*, /health
  • 401 on mismatch

rate_limit_middleware:
  • Token bucket per client IP
  • Default: 20 requests/min, burst of 10
  • Returns 429 with Retry-After header when exceeded
  • Skip for /dashboard/*, /health

security_middleware:
  • Request body size cap (default 10MB, configurable --max-body)
  • Returns 413 Payload Too Large on exceed
  • Adds response headers: X-Content-Type-Options: nosniff
```

#### `stats.py` — Metrics + SSE Broadcasting
```
class StatsCollector:
  Tracked metrics:
    • start_time (for uptime calculation)
    • total / ok / fail request counts
    • active stream count (incremented on start, decremented in finally)
    • bytes_up (request bodies) / bytes_down (response bodies)
    • per-client dict: {ip: {reqs, bytes, first_seen, last_seen}}
    • request_log: deque(maxlen=200) — ring buffer of recent requests
    • token_refreshes count + last_refresh timestamp

  Methods:
    • record(entry)      — log a completed request + update counters
    • snapshot() → dict  — current stats as JSON-serializable dict
    • add_client(q)      — register an SSE client queue
    • remove_client(q)   — unregister
    • broadcast(evt, data) — push to all SSE client queues

  SSE Protocol:
    • event: stats    — full stats snapshot (sent on connect + on changes)
    • event: request  — single request log entry (sent after each proxy request)
    • Queue per client, maxsize=50, drop on full (prevents slow client from blocking)
```

#### `dashboard.html` — Frontend
```
Single HTML file, no external dependencies, no build step.

Structure:
  <head>
    • Inline <style> — dark theme, CSS grid, responsive
    • Color palette: bg #0d1117, cards #161b22, border #30363d,
      text #e6edf3, accent #58a6ff, green #3fb950, red #f85149, amber #d29922

  <body>
    • Header: title + connection status indicator (green dot = connected)
    • Stats cards: 5-column grid (uptime, requests, active, errors, token)
    • Request log: <table> with auto-scroll, max 50 visible rows
    • Clients table: per-IP breakdown

  <script>
    • SSEManager:
        - new EventSource('/dashboard/events')
        - on 'stats' event → update cards, clients table
        - on 'request' event → prepend row to log table
        - on error → show offline indicator, auto-reconnect (EventSource does this)
    • Formatters:
        - formatUptime(seconds) → "2h 14m"
        - formatBytes(n) → "12.4 MB"
        - formatTime(unix) → "14:32:01"
        - formatAgo(unix) → "3m ago"
    • Status color: 2xx green, 4xx amber, 5xx red
    • Auto-trim log table to 50 rows (remove oldest)
```

#### `requirements.txt`
```
aiohttp>=3.9
```

#### `proxy.service` — Systemd User Service
```
[Unit]
Description=Claude Code Proxy
After=network-online.target tailscaled.service

[Service]
ExecStart=/usr/bin/python3 /home/grace/proxy/server.py --port 9191 --secret <SECRET>
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target

Install: systemctl --user enable proxy.service
Start:   systemctl --user start proxy.service
Logs:    journalctl --user -u proxy.service -f
```

---

## Request Flow (detailed)

```
Client Claude Code                    Proxy                              Anthropic
       │                                │                                    │
       │  POST /v1/messages             │                                    │
       │  x-api-key: <proxy-secret>     │                                    │
       │  anthropic-version: 2023-06-01 │                                    │
       │  content-type: application/json│                                    │
       │  {"model":"...", "stream":true} │                                    │
       │ ──────────────────────────────>│                                    │
       │                                │                                    │
       │                    ┌───────────┤                                    │
       │                    │ security  │                                    │
       │                    │ middleware│ check body size ≤ 10MB             │
       │                    ├───────────┤                                    │
       │                    │ rate limit│ check token bucket for client IP   │
       │                    ├───────────┤                                    │
       │                    │ auth      │ validate x-api-key == secret       │
       │                    └───────────┤                                    │
       │                                │                                    │
       │                    ┌───────────┤                                    │
       │                    │  token    │ check expiresAt > now + 60s        │
       │                    │  manager  │ if expired: POST refresh to        │
       │                    │           │ platform.claude.com/v1/oauth/token │
       │                    └───────────┤                                    │
       │                                │                                    │
       │                                │  POST /v1/messages                 │
       │                                │  Authorization: Bearer <oauth>     │
       │                                │  (same body, filtered headers)     │
       │                                │ ──────────────────────────────────>│
       │                                │                                    │
       │                                │  200 OK                            │
       │                                │  content-type: text/event-stream   │
       │                                │  data: {"type":"content_block_...} │
       │                                │ <──────────────────────────────────│
       │                                │                                    │
       │  200 OK                        │        (chunk by chunk,            │
       │  content-type: text/event-stream        with drain() backpressure)  │
       │  data: {"type":"content_block_...}      │                           │
       │ <─────────────────────────────│                                    │
       │         ...                    │         ...                         │
       │  data: [DONE]                  │  data: [DONE]                      │
       │ <─────────────────────────────│ <──────────────────────────────────│
       │                                │                                    │
       │                    ┌───────────┤                                    │
       │                    │  stats    │ record: method, path, status,      │
       │                    │  collect  │ duration_ms, bytes, client_ip      │
       │                    ├───────────┤                                    │
       │                    │  SSE      │ broadcast "request" event to       │
       │                    │  broadcast│ dashboard clients                  │
       │                    └───────────┘                                    │
```

---

## Edge Cases Handled

| Edge Case | How It's Handled |
|---|---|
| Token expires mid-stream | Tokens validated before stream starts; streams to Anthropic don't re-check auth mid-flight |
| Token expires between requests | Pre-emptive refresh 60s before expiry |
| Two requests trigger refresh simultaneously | asyncio.Lock prevents double-refresh |
| Claude Code on same device also refreshes token | File mtime watch picks up external credential changes |
| Client disconnects mid-stream | `finally` block in proxy handler closes upstream connection |
| Upstream closes connection mid-stream | `ClientPayloadError` caught; partial response sent, then EOF |
| Upstream returns 401 (stale token) | Force-refresh token, retry request once |
| Upstream returns 429 (rate limited) | Pass through to client with original Retry-After header |
| Slow client can't consume chunks fast enough | `response.drain()` applies backpressure, pauses upstream reads |
| Oversized request body | Middleware rejects with 413 before proxying |
| Credentials file missing/corrupt | Return 502 with clear error message |
| Proxy shutdown with active streams | `on_shutdown` hook waits up to 60s for in-flight requests to drain |
| SSE dashboard client disconnects | Queue removed from broadcast list; dead queues auto-pruned |
| Multiple dashboard tabs open | Each gets its own SSE queue, all receive broadcasts |

---

## OAuth Authentication Details (discovered via reverse engineering)

Claude Code Max uses OAuth Bearer tokens, NOT API keys. The proxy must handle this:

1. **Required beta flags**: Every request MUST include `anthropic-beta: claude-code-20250219,oauth-2025-04-20` — without `oauth-2025-04-20`, the API rejects Bearer tokens with "OAuth authentication is currently not supported"

2. **Billing header**: Every request body MUST contain a system message with `x-anthropic-billing-header: cc_version=2.1.78; cc_entrypoint=cli;` — without it, the API returns 400 "Error"

3. **No compression**: The proxy strips `Accept-Encoding` from client requests and uses `auto_decompress=False` on the upstream session to avoid gzip/zlib issues when re-streaming

4. **Token refresh**: Relies on Claude Code running on the proxy device to refresh tokens (writes to `~/.claude/.credentials.json`). The proxy watches file mtime for changes.

---

## Client Setup (on other devices)

```bash
# Add to shell profile (~/.bashrc, ~/.zshrc, etc.)
export ANTHROPIC_BASE_URL="http://<tailscale-ip>:9191"
export ANTHROPIC_API_KEY="<your-proxy-secret>"

# Then just run claude as normal
claude
```
