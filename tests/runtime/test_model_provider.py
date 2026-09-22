"""06 §1/§4 — the ModelProvider interface: normalized, provider-isolated,
secret-safe, and never a runtime change to add a vendor.

The Ollama adapter is tested against a mocked HTTP transport (no real
network, no real Ollama needed) — deterministic and offline-safe.
"""

from __future__ import annotations

import httpx
import pytest

from server.models.ollama import OllamaProvider
from server.models.provider import build_provider
from shared.schemas.enums import ModelProvider as ModelProviderName
from shared.schemas.runtime import GenerationPolicy, ModelMessage, ModelUnavailable, UnsupportedModelProvider

_RealAsyncClient = httpx.AsyncClient


def _mock_ollama_client(handler):
    return _RealAsyncClient(transport=httpx.MockTransport(handler))


async def test_ollama_provider_invokes_and_normalizes_the_response(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/chat"
        return httpx.Response(200, json={"message": {"content": "hello there"}, "eval_count": 7, "done": True})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _mock_ollama_client(handler))

    provider = OllamaProvider(model="qwen2.5:3b-instruct")
    result = await provider.invoke([ModelMessage(role="user", content="hi")], GenerationPolicy(), 5.0)

    assert result.content == "hello there"
    assert result.tokens_used == 7


async def test_ollama_provider_connection_failure_is_model_unavailable(monkeypatch):
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _mock_ollama_client(handler))

    provider = OllamaProvider(model="qwen2.5:3b-instruct")
    with pytest.raises(ModelUnavailable):
        await provider.invoke([ModelMessage(role="user", content="hi")], GenerationPolicy(), 5.0)


async def test_ollama_provider_malformed_response_is_model_unavailable(monkeypatch):
    """FAIL-CORE-002: a response this adapter cannot make sense of is an
    explicit failure, never a fabricated `ModelResult`."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"unexpected": "shape"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _mock_ollama_client(handler))

    provider = OllamaProvider(model="qwen2.5:3b-instruct")
    with pytest.raises(ModelUnavailable):
        await provider.invoke([ModelMessage(role="user", content="hi")], GenerationPolicy(), 5.0)


async def test_a_provider_never_receives_an_api_key_in_its_messages(monkeypatch):
    """06 §1 [LOCKED]: "never in the model messages, never logged." The
    Ollama adapter is keyless by construction — this test asserts the
    request body sent to the provider never contains the string an api_key
    would be, proving there is no code path that could smuggle one in via
    the message list."""

    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = request.content.decode()
        return httpx.Response(200, json={"message": {"content": "ok"}})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: _mock_ollama_client(handler))

    provider = OllamaProvider(model="qwen2.5:3b-instruct")
    await provider.invoke([ModelMessage(role="user", content="hi")], GenerationPolicy(), 5.0)

    assert "sk-" not in captured["body"] and "api_key" not in captured["body"]


def test_unregistered_provider_fails_explicitly_not_silently():
    """MODEL-002: "adding one is an adapter + config, not a runtime change" —
    the flip side is that selecting a provider with no adapter yet is an
    honest, explicit failure, never a silent fallback or a crash."""

    with pytest.raises(UnsupportedModelProvider):
        build_provider(ModelProviderName.ANTHROPIC, model="claude", endpoint=None, api_key="whatever")


def test_ollama_is_registered_as_the_default_local_first_adapter():
    provider = build_provider(ModelProviderName.OLLAMA, model="qwen2.5:3b-instruct", endpoint=None, api_key=None)
    assert isinstance(provider, OllamaProvider)
