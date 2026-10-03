"""Browser Use's model, inside the container: the JARVIS Model Gateway.

A `BaseChatModel` for Browser Use 0.13.x that sends every model call to the
run's Model Gateway socket (`/run/jarvis/model.sock`) with the run's model
token — the only credential in the container (docs/29 §21 item 3). It holds
no provider key, names no provider or model (only the alias `agent-model`),
and sends text only. Structured output travels as a schema in the system
prompt, because the gateway accepts no `response_format` (docs/29 §12.2,
register AF-P6-3).

Everything the gateway answers is untrusted data to Browser Use, and
everything Browser Use asks is decided by the gateway: the alias, the run,
the budgets, the bounds.
"""

from __future__ import annotations

from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from browser_use.llm.messages import BaseMessage
from browser_use.llm.schema import SchemaOptimizer
from browser_use.llm.views import ChatInvokeCompletion, ChatInvokeUsage

from protocol import MODEL_ALIAS, extract_json, gateway_request, with_schema

T = TypeVar("T", bound=BaseModel)


class JarvisGatewayError(Exception):
    pass


class JarvisChatModel:
    """Browser Use's `BaseChatModel` protocol, over the gateway socket."""

    _verified_api_keys = True

    def __init__(self, *, socket_path: str, run_token: str, timeout: float = 120.0) -> None:
        self.model = MODEL_ALIAS
        self._client = httpx.AsyncClient(
            transport=httpx.AsyncHTTPTransport(uds=socket_path), base_url="http://jarvis-model-gateway",
            headers={"Authorization": f"Bearer {run_token}"}, timeout=timeout, trust_env=False)

    @property
    def provider(self) -> str:
        return "jarvis"

    @property
    def name(self) -> str:
        return MODEL_ALIAS

    @property
    def model_name(self) -> str:
        return MODEL_ALIAS

    async def ainvoke(self, messages: list[BaseMessage], output_format: type[T] | None = None,
                      **_kwargs: Any) -> ChatInvokeCompletion:
        pairs = [(getattr(m, "role", "user"), getattr(m, "text", str(m))) for m in messages]
        if output_format is not None:
            pairs = with_schema(pairs, SchemaOptimizer.create_optimized_json_schema(output_format))
        response = await self._client.post("/v1/chat/completions", json=gateway_request(pairs))
        if response.status_code != 200:
            code = (response.json().get("error") or {}).get("code", "refused") if response.content else "refused"
            raise JarvisGatewayError(f"model gateway refused: {response.status_code} {code}")
        body = response.json()
        choice = body["choices"][0]
        text = choice["message"]["content"] or ""
        usage = body.get("usage") or {}
        stats = ChatInvokeUsage(prompt_tokens=int(usage.get("prompt_tokens", 0)), prompt_cached_tokens=None,
                                prompt_cache_creation_tokens=None, prompt_image_tokens=None,
                                completion_tokens=int(usage.get("completion_tokens", 0)),
                                total_tokens=int(usage.get("total_tokens", 0)))
        if output_format is None:
            return ChatInvokeCompletion(completion=text, usage=stats, stop_reason=choice.get("finish_reason"))
        parsed = output_format.model_validate_json(extract_json(text))
        return ChatInvokeCompletion(completion=parsed, usage=stats, stop_reason=choice.get("finish_reason"))

    async def aclose(self) -> None:
        await self._client.aclose()

    @classmethod
    def __get_pydantic_core_schema__(cls, source_type: type, handler: Any) -> Any:
        # Browser Use keeps its model inside pydantic settings (the protocol's
        # own hook): any object, as the protocol itself declares.
        from pydantic_core import core_schema

        return core_schema.any_schema()


__all__ = ["JarvisChatModel", "JarvisGatewayError"]
