"""Agent runtime providers (docs/29 §7) — the engines, never the authority.

Phase 1 declares the boundary only (`base.py`). The native provider arrives
with Phase 2; external providers (Letta, Browser Use, OpenHands, …) are
`[BLOCKED BY INFRASTRUCTURE]` (docs/29 §21) and none is imported anywhere —
third-party agent frameworks are never loaded into the gateway process.
Import contract AF-C3: nothing here can reach the authorization engine, the
grant tables or the SecretStore.
"""
