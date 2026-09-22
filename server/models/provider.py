"""The normalized model interface (06 §1).

```
interface ModelProvider:
    invoke(messages, generation_policy, timeout) -> ModelResult
    health() -> ok | unavailable
```

Provider quirks never leak upward: every adapter translates to and from these
types. Nothing here can carry authority — a `ModelResult` is text plus token
counts, and the runtime treats the text as a *proposal* to parse (P1), never as
an instruction to follow.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Mapping, Protocol, Sequence

# Resolves the adapter's own declared `secret_ref` at the moment of use.
KeyProvider = Callable[[], Awaitable[str]]


class ModelUnavailable(Exception):
    """The provider could not produce a result (FAIL-005).

    The message is written for operators and clients alike, so it never
    contains a key, a request body, or a provider response body — only the
    provider name and a coarse reason.
    """


@dataclass(frozen=True)
class ChatMessage:
    role: str  # "system" | "user" | "assistant"
    content: str


@dataclass(frozen=True)
class ModelPricing:
    input_per_1k_tokens: float = 0.0
    output_per_1k_tokens: float = 0.0

    @property
    def is_paid(self) -> bool:
        return self.input_per_1k_tokens > 0 or self.output_per_1k_tokens > 0

    def cost(self, *, prompt_tokens: int, completion_tokens: int) -> float:
        return (
            max(0, prompt_tokens) / 1000.0 * self.input_per_1k_tokens
            + max(0, completion_tokens) / 1000.0 * self.output_per_1k_tokens
        )


@dataclass(frozen=True)
class ModelSpec:
    """Which model, where, and at what price — never the credential itself."""

    provider: str
    model: str
    endpoint: str | None = None
    timeout_seconds: float = 60.0
    pricing: ModelPricing = field(default_factory=ModelPricing)
    generation_policy: Mapping[str, object] = field(default_factory=dict)

    def projected_cost(self, *, prompt_chars: int) -> float:
        """A deterministic upper-bound estimate *before* a call, for the budget
        check (13 §3). Prompt tokens are approximated as chars/4; completion
        tokens as the configured `max_tokens` (or 1024). Local models cost 0."""

        if not self.pricing.is_paid:
            return 0.0
        max_tokens = self.generation_policy.get("max_tokens", 1024)
        try:
            completion = int(max_tokens)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            completion = 1024
        return self.pricing.cost(prompt_tokens=prompt_chars // 4 + 1, completion_tokens=completion)


@dataclass(frozen=True)
class ModelResult:
    content: str
    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return max(0, self.prompt_tokens) + max(0, self.completion_tokens)


class ModelProvider(Protocol):
    spec: ModelSpec

    async def invoke(
        self, messages: Sequence[ChatMessage], *, timeout: float
    ) -> ModelResult: ...

    async def health(self) -> bool: ...
