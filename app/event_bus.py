from __future__ import annotations

import json
from fastapi import WebSocket


class EventBus:
    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self.connections.add(websocket)

    def disconnect(self, websocket: WebSocket) -> None:
        self.connections.discard(websocket)

    async def broadcast(self, event: str, payload: dict | None = None) -> None:
        if not self.connections:
            return
        data = json.dumps({"event": event, "payload": payload or {}}, ensure_ascii=False)
        dead = []
        for ws in list(self.connections):
            try:
                await ws.send_text(data)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.disconnect(ws)


events = EventBus()
