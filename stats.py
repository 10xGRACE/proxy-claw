"""Metrics collection and SSE broadcasting for the dashboard."""

import asyncio
import json
import time
from collections import deque
from dataclasses import dataclass, field


@dataclass
class RequestEntry:
    ts: float
    method: str
    path: str
    client: str
    status: int
    ms: float
    bytes: int
    error: str | None = None
    request_id: str = ""

    def to_dict(self) -> dict:
        d = {
            "ts": self.ts, "method": self.method, "path": self.path,
            "client": self.client, "status": self.status,
            "ms": self.ms, "bytes": self.bytes, "request_id": self.request_id,
        }
        if self.error:
            d["error"] = self.error
        return d


class StatsCollector:
    def __init__(self):
        self.start_time = time.time()
        self.total = 0
        self.ok = 0
        self.fail = 0
        self.active = 0
        self.bytes_up = 0
        self.bytes_down = 0
        self.refreshes = 0
        self.last_refresh = 0.0
        self.log: deque[RequestEntry] = deque(maxlen=200)
        self.clients: dict[str, dict] = {}
        self._sse_queues: list[asyncio.Queue] = []

    def reset(self):
        self.total = 0
        self.ok = 0
        self.fail = 0
        self.active = 0
        self.bytes_up = 0
        self.bytes_down = 0
        self.refreshes = 0
        self.last_refresh = 0.0
        self.log.clear()
        self.clients.clear()

    def record(self, entry: RequestEntry):
        self.total += 1
        self.bytes_down += entry.bytes
        if entry.status < 400:
            self.ok += 1
        else:
            self.fail += 1

        ip = entry.client
        if ip not in self.clients:
            self.clients[ip] = {"reqs": 0, "bytes": 0, "first": time.time(), "last": 0}
        self.clients[ip]["reqs"] += 1
        self.clients[ip]["bytes"] += entry.bytes
        self.clients[ip]["last"] = time.time()

        self.log.append(entry)

    def snapshot(self, token_expires: int = 0) -> dict:
        return {
            "uptime": round(time.time() - self.start_time),
            "total": self.total,
            "ok": self.ok,
            "fail": self.fail,
            "active": self.active,
            "bytes_up": self.bytes_up,
            "bytes_down": self.bytes_down,
            "token_expires": token_expires,
            "token_valid": time.time() * 1000 < token_expires if token_expires else False,
            "refreshes": self.refreshes,
            "clients": self.clients,
            "log": [e.to_dict() for e in list(self.log)[-50:]],
        }

    # ── SSE ──────────────────────────────────────────────

    def add_sse_client(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=64)
        self._sse_queues.append(q)
        return q

    def remove_sse_client(self, q: asyncio.Queue):
        if q in self._sse_queues:
            self._sse_queues.remove(q)

    async def broadcast(self, event: str, data: dict):
        msg = f"event: {event}\ndata: {json.dumps(data)}\n\n"
        dead = []
        for q in self._sse_queues:
            try:
                q.put_nowait(msg)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._sse_queues.remove(q)
