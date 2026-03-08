"""
WebSocket connection manager — broadcasts JSON events to all connected browsers.
"""

from __future__ import annotations
import asyncio
import json
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import WebSocket


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class ConnectionManager:
    def __init__(self) -> None:
        self._clients: list["WebSocket"] = []
        self._lock = asyncio.Lock()

    async def connect(self, ws: "WebSocket") -> None:
        await ws.accept()
        async with self._lock:
            self._clients.append(ws)

    async def disconnect(self, ws: "WebSocket") -> None:
        async with self._lock:
            self._clients = [c for c in self._clients if c is not ws]

    async def broadcast(self, event: str, data: dict) -> None:
        if not self._clients:
            return
        payload = json.dumps({"event": event, "data": data, "ts": _now_iso()})
        dead: list["WebSocket"] = []
        for client in list(self._clients):
            try:
                await client.send_text(payload)
            except Exception:
                dead.append(client)
        for ws in dead:
            await self.disconnect(ws)

    @property
    def count(self) -> int:
        return len(self._clients)


# Module-level singleton — import this everywhere
manager = ConnectionManager()
