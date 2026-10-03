"""What the internal Model Gateway listener needs (docs/29 §12, Phase 6 slice 6A).

`[PROPOSAL — NOT CANONICAL UNTIL RATIFIED]` (docs/29). The HTTP face of the
Model Gateway, for an external runtime (OD-AF-6: Browser Use) that must never
hold a provider key. `server.gateway` sits below `server.agents` (16 §2), so
it declares this Protocol and the composition root supplies the
implementation (`server/composition/model_gateway.py`).

The listener hands over exactly what arrived — the `Authorization` header,
the raw body, and a way to ask whether the caller has gone — and returns
what the implementation answered. It parses nothing, decides nothing and
holds no session of its own.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Protocol

Disconnected = Callable[[], Awaitable[bool]]

# Not an HTTP status a client ever reads: the caller is gone (nginx's 499).
CLIENT_CLOSED = 499


@dataclass(frozen=True)
class ModelGatewayReply:
    status: int
    payload: Mapping[str, Any] = field(default_factory=dict)


class ModelGatewayPort(Protocol):
    async def chat_completion(self, *, authorization: str | None, body: bytes,
                              disconnected: Disconnected, run_id: "uuid.UUID | None" = None) -> ModelGatewayReply: ...


__all__ = ["CLIENT_CLOSED", "Disconnected", "ModelGatewayPort", "ModelGatewayReply"]
