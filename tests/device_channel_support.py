"""Test support for the device channel (docs/23 §4).

`AsgiWebSocket` drives the real FastAPI WebSocket endpoint in-process, on the
test's own event loop (so it shares the async SQLite storage the rest of the
app uses) — the ASGI messages are exactly what a server like uvicorn would
exchange with the app for a real socket.

`FakeConnection` stands in for a socket when a test exercises the hub alone.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field


class ChannelClosed(Exception):
    def __init__(self, code: int, reason: str = "") -> None:
        super().__init__(f"closed {code} {reason}")
        self.code = code
        self.reason = reason


class AsgiWebSocket:
    def __init__(self, app, path: str = "/api/v1/devices/channel") -> None:
        self._app = app
        self._path = path
        self._inbound: asyncio.Queue = asyncio.Queue()
        self._outbound: asyncio.Queue = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self.close_code: int | None = None

    async def connect(self) -> None:
        scope = {
            "type": "websocket",
            "asgi": {"version": "3.0"},
            "scheme": "ws",
            "path": self._path,
            "raw_path": self._path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 12345),
            "server": ("test", 80),
            "subprotocols": [],
            "state": {},
        }

        async def receive():
            return await self._inbound.get()

        async def send(message):
            await self._outbound.put(message)

        self._task = asyncio.ensure_future(self._app(scope, receive, send))
        await self._inbound.put({"type": "websocket.connect"})
        message = await asyncio.wait_for(self._outbound.get(), 5)
        if message["type"] == "websocket.close":
            self.close_code = message.get("code")
            raise ChannelClosed(message.get("code"), message.get("reason", ""))
        assert message["type"] == "websocket.accept", message

    async def send_text(self, text: str) -> None:
        await self._inbound.put({"type": "websocket.receive", "text": text})

    async def send_json(self, payload: dict) -> None:
        await self.send_text(json.dumps(payload))

    async def receive_text(self, timeout: float = 5.0) -> str:
        message = await asyncio.wait_for(self._outbound.get(), timeout)
        if message["type"] == "websocket.close":
            self.close_code = message.get("code")
            raise ChannelClosed(message.get("code"), message.get("reason", ""))
        return message["text"]

    async def receive_json(self, timeout: float = 5.0) -> dict:
        return json.loads(await self.receive_text(timeout))

    async def expect_close(self, timeout: float = 5.0) -> int:
        while True:
            try:
                await self.receive_text(timeout)
            except ChannelClosed as closed:
                return closed.code

    async def disconnect(self) -> None:
        await self._inbound.put({"type": "websocket.disconnect", "code": 1000})
        if self._task is not None:
            await asyncio.wait_for(self._task, 5)

    async def finish(self) -> None:
        if self._task is not None and not self._task.done():
            await self._inbound.put({"type": "websocket.disconnect", "code": 1000})
            await asyncio.wait_for(self._task, 5)


@dataclass
class FakeConnection:
    sent: list[str] = field(default_factory=list)
    closed: tuple[int, str] | None = None
    fail_sends: bool = False
    arrived: asyncio.Event = field(default_factory=asyncio.Event)

    async def send_text(self, text: str) -> None:
        if self.fail_sends or self.closed is not None:
            raise ConnectionError("closed")
        self.sent.append(text)
        self.arrived.set()

    async def close(self, code: int, reason: str) -> None:
        self.closed = (code, reason)

    def frames(self) -> list[dict]:
        return [json.loads(t) for t in self.sent]

    async def wait_frames(self, count: int, timeout: float = 5.0) -> list[dict]:
        """Wait until at least `count` frames have been sent; return them all."""

        async def _wait() -> None:
            while len(self.sent) < count:
                self.arrived.clear()
                if len(self.sent) >= count:
                    break
                await self.arrived.wait()

        await asyncio.wait_for(_wait(), timeout)
        return self.frames()
