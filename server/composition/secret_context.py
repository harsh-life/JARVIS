"""Request-scoped secret resolution for model adapters (06 §1, 12 §2, 16 §3).

A model adapter resolves its own declared `secret_ref` at the moment of the call.
Resolution needs the request's transaction and audit writer, but model-tool
providers are long-lived objects in the tool registry. So each request installs
a resolver in a `ContextVar`, and a provider's `KeyProvider` reads the current
one when it is invoked. Outside a request there is no resolver, and the call
fails closed (FAIL-012) — there is no fallback to a raw value.

Two `secret_ref` forms are accepted (15 §2): `env:NAME` reads the environment at
use; `secretstore:<handle>` goes through `SecretStore.get` with the requester
the configuration's scope implies. The key is returned to the adapter and never
stored here.
"""

from __future__ import annotations

import os
from contextvars import ContextVar
from typing import Awaitable, Callable

from server.models.provider import KeyProvider
from server.secrets.requester import SecretRequester
from server.secrets.store import store_handle

SecretResolver = Callable[[str, SecretRequester], Awaitable[str]]

CURRENT_SECRET_RESOLVER: ContextVar[SecretResolver | None] = ContextVar(
    "hypermind_secret_resolver", default=None
)


class SecretUnavailable(Exception):
    """No resolution was possible. Carries no detail about the secret."""


def key_provider_for(secret_ref: str | None, requester: SecretRequester) -> KeyProvider | None:
    if not secret_ref:
        return None

    # Only `env:` reads the environment — anything else names a SecretStore
    # handle. A config-file reference (`SecretRef`, 15 §2) is `secretstore:<id>`,
    # which *is* the store's handle (handles carry their own prefix); a stored
    # AgentConfiguration row carries one extra prefix by design
    # (model_config._row_ref). `store_handle` maps both to the handle and
    # rewrites nothing else; an unknown result simply fails at the store.
    if secret_ref.startswith("env:"):
        env_name = secret_ref[len("env:") :]

        async def _from_env() -> str:
            value = os.environ.get(env_name)
            if not value:
                raise SecretUnavailable()
            return value

        return _from_env

    handle = store_handle(secret_ref)

    async def _from_store() -> str:
        resolver = CURRENT_SECRET_RESOLVER.get()
        if resolver is None:
            raise SecretUnavailable()
        return await resolver(handle, requester)

    return _from_store
