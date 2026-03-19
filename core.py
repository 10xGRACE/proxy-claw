"""TokenManager and ProxyHandler — the core proxy logic."""

import asyncio
import json
import logging
import time
from pathlib import Path

import aiohttp
from aiohttp import web

from stats import RequestEntry, StatsCollector

ANTHROPIC_API = "https://api.anthropic.com"
CREDS_PATH = Path.home() / ".claude" / ".credentials.json"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
CLIENT_ID = "claude-code-20250219"

# Required headers/body fields for OAuth-based requests
REQUIRED_BETA_FLAGS = {"claude-code-20250219", "oauth-2025-04-20"}
BILLING_HEADER = "x-anthropic-billing-header: cc_version=2.1.78; cc_entrypoint=cli;"

# Headers that must not be forwarded between hops
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade",
})

log = logging.getLogger("proxy.core")


class TokenManager:
    """Loads, caches, and auto-refreshes OAuth credentials."""

    def __init__(self, session: aiohttp.ClientSession, stats: StatsCollector):
        self._session = session
        self._stats = stats
        self._creds: dict = {}
        self._mtime = 0.0
        self._lock = asyncio.Lock()

    def _load(self):
        try:
            mt = CREDS_PATH.stat().st_mtime
            if mt != self._mtime:
                raw = json.loads(CREDS_PATH.read_text())
                self._creds = raw.get("claudeAiOauth", raw)
                self._mtime = mt
                log.info("credentials loaded (expires in %ds)",
                         max(0, int((self._creds.get("expiresAt", 0) / 1000) - time.time())))
        except Exception as e:
            log.error("credential load failed: %s", e)

    async def _refresh(self):
        async with self._lock:
            # Re-check after acquiring lock — another coroutine may have refreshed
            self._load()
            if time.time() * 1000 < self._creds.get("expiresAt", 0) - 30_000:
                return

            log.info("refreshing oauth token…")
            try:
                scopes = self._creds.get("scopes", [])
                scope_str = " ".join(scopes) if scopes else (
                    "user:profile user:inference user:sessions:claude_code "
                    "user:mcp_servers user:file_upload"
                )
                async with self._session.post(
                    TOKEN_URL,
                    json={
                        "grant_type": "refresh_token",
                        "refresh_token": self._creds["refreshToken"],
                        "client_id": CLIENT_ID,
                        "scope": scope_str,
                    },
                    headers={
                        "Content-Type": "application/json",
                        "User-Agent": "claude-cli/2.1.78",
                    },
                    timeout=aiohttp.ClientTimeout(total=30),
                ) as r:
                    if r.status != 200:
                        body = await r.text()
                        log.error("token refresh failed (%d): %s", r.status, body)
                        raise RuntimeError(f"token refresh returned {r.status}")

                    d = await r.json()
                    self._creds["accessToken"] = d["access_token"]
                    if "refresh_token" in d:
                        self._creds["refreshToken"] = d["refresh_token"]
                    self._creds["expiresAt"] = int(time.time() * 1000) + d.get("expires_in", 3600) * 1000

                    # Write back so Claude Code on this device stays in sync
                    raw = json.loads(CREDS_PATH.read_text())
                    raw["claudeAiOauth"] = self._creds
                    CREDS_PATH.write_text(json.dumps(raw, indent=2))
                    self._mtime = CREDS_PATH.stat().st_mtime

                    self._stats.refreshes += 1
                    self._stats.last_refresh = time.time()
                    log.info("token refreshed (expires in %ds)",
                             int((self._creds["expiresAt"] / 1000) - time.time()))

            except RuntimeError:
                raise
            except Exception as e:
                log.error("token refresh error: %s", e)
                raise

    async def get_token(self) -> str:
        self._load()
        if not self._creds.get("accessToken"):
            raise RuntimeError("no credentials found — run `claude login` on this device")
        # Refresh if within 60s of expiry
        if time.time() * 1000 > self._creds.get("expiresAt", 0) - 60_000:
            await self._refresh()
        return self._creds["accessToken"]

    def expires_at(self) -> int:
        self._load()
        return self._creds.get("expiresAt", 0)

    async def force_refresh(self):
        """Force a token refresh (called on 401 from upstream)."""
        self._creds["expiresAt"] = 0  # Force expiry
        await self._refresh()


class ProxyHandler:
    """Forwards requests to api.anthropic.com with OAuth injection."""

    def __init__(self, session: aiohttp.ClientSession, token_mgr: TokenManager, stats: StatsCollector):
        self._session = session
        self._tokens = token_mgr
        self._stats = stats

    async def handle(self, request: web.Request) -> web.StreamResponse:
        t0 = time.time()
        self._stats.active += 1
        client_ip = request.remote or "unknown"

        try:
            return await self._do_proxy(request, client_ip, t0, retry_on_401=True)
        finally:
            self._stats.active -= 1
            await self._stats.broadcast("stats", self._stats.snapshot(self._tokens.expires_at()))

    async def _do_proxy(
        self, request: web.Request, client_ip: str, t0: float, *, retry_on_401: bool
    ) -> web.StreamResponse:
        try:
            token = await self._tokens.get_token()

            # Build upstream headers — strip auth + hop-by-hop
            headers = {}
            skip = HOP_BY_HOP | {"host", "x-api-key", "authorization", "content-length", "accept-encoding"}
            for k, v in request.headers.items():
                if k.lower() not in skip:
                    headers[k] = v
            headers["Authorization"] = f"Bearer {token}"
            headers["Host"] = "api.anthropic.com"

            # Ensure required beta flags are present for OAuth auth
            existing_beta = headers.get("anthropic-beta", "")
            beta_parts = [b.strip() for b in existing_beta.split(",") if b.strip()] if existing_beta else []
            for flag in REQUIRED_BETA_FLAGS:
                if flag not in beta_parts:
                    beta_parts.append(flag)
            headers["anthropic-beta"] = ",".join(beta_parts)

            body = await request.read()

            # Inject billing header into JSON body if missing (required for OAuth)
            body = _ensure_billing_header(body)

            self._stats.bytes_up += len(body)

            url = f"{ANTHROPIC_API}{request.path}"
            if request.query_string:
                url += f"?{request.query_string}"

            timeout = aiohttp.ClientTimeout(total=600, sock_read=300)
            async with self._session.request(
                request.method, url, headers=headers,
                data=body or None, timeout=timeout,
            ) as upstream:

                # 401 → refresh token and retry once
                if upstream.status == 401 and retry_on_401:
                    log.warning("upstream 401 — refreshing token and retrying")
                    await self._tokens.force_refresh()
                    return await self._do_proxy(request, client_ip, t0, retry_on_401=False)

                # Build response — strip hop-by-hop headers
                resp_headers = {}
                for k, v in upstream.headers.items():
                    if k.lower() not in HOP_BY_HOP and k.lower() != "content-length":
                        resp_headers[k] = v

                response = web.StreamResponse(status=upstream.status, headers=resp_headers)
                await response.prepare(request)

                sent = 0
                try:
                    async for chunk in upstream.content.iter_any():
                        await response.write(chunk)
                        await response.drain()  # backpressure
                        sent += len(chunk)
                except (ConnectionResetError, aiohttp.ClientConnectionResetError):
                    pass  # client disconnected mid-stream

                try:
                    await response.write_eof()
                except (ConnectionResetError, aiohttp.ClientConnectionResetError):
                    pass

                ms = (time.time() - t0) * 1000
                entry = RequestEntry(
                    ts=time.time(), method=request.method, path=request.path,
                    client=client_ip, status=upstream.status, ms=round(ms, 1), bytes=sent,
                )
                self._stats.record(entry)
                log.info("%s %s → %d (%.0fms, %s)", request.method, request.path,
                         upstream.status, ms, _fmt_bytes(sent))
                await self._stats.broadcast("request", entry.to_dict())
                return response

        except aiohttp.ClientConnectorError as e:
            return self._error(request, client_ip, t0, 502, f"upstream unreachable: {e}")
        except asyncio.TimeoutError:
            return self._error(request, client_ip, t0, 504, "upstream timeout")
        except aiohttp.ClientPayloadError as e:
            return self._error(request, client_ip, t0, 502, f"upstream closed mid-stream: {e}")
        except aiohttp.ServerDisconnectedError:
            return self._error(request, client_ip, t0, 502, "upstream disconnected")
        except RuntimeError as e:
            return self._error(request, client_ip, t0, 502, str(e))
        except asyncio.CancelledError:
            # Client disconnected — let it propagate after cleanup logging
            ms = (time.time() - t0) * 1000
            log.info("client disconnected: %s %s (%.0fms)", request.method, request.path, ms)
            raise

    def _error(self, request: web.Request, client_ip: str, t0: float,
               status: int, message: str) -> web.Response:
        ms = (time.time() - t0) * 1000
        entry = RequestEntry(
            ts=time.time(), method=request.method, path=request.path,
            client=client_ip, status=status, ms=round(ms, 1), bytes=0, error=message,
        )
        self._stats.record(entry)
        log.error("%s %s → %d: %s (%.0fms)", request.method, request.path, status, message, ms)
        # Fire-and-forget broadcast — we're returning a response, not streaming
        asyncio.ensure_future(self._stats.broadcast("request", entry.to_dict()))
        return web.json_response({"error": message}, status=status)


def _ensure_billing_header(body: bytes) -> bytes:
    """Inject billing system message if the request body is JSON and lacks one."""
    if not body:
        return body
    try:
        data = json.loads(body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return body

    if not isinstance(data, dict):
        return body

    # Check if billing header already present in system messages
    system = data.get("system", [])
    if isinstance(system, str):
        if "x-anthropic-billing-header" in system:
            return body
        # Convert string system to list format and prepend billing
        data["system"] = [
            {"type": "text", "text": BILLING_HEADER},
            {"type": "text", "text": system},
        ]
    elif isinstance(system, list):
        for item in system:
            if isinstance(item, dict) and "x-anthropic-billing-header" in item.get("text", ""):
                return body
        # Prepend billing header
        data["system"] = [{"type": "text", "text": BILLING_HEADER}] + system
    else:
        data["system"] = [{"type": "text", "text": BILLING_HEADER}]

    return json.dumps(data, separators=(",", ":")).encode()


def _fmt_bytes(n: int) -> str:
    for u in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {u}"
        n /= 1024
    return f"{n:.1f} TB"
