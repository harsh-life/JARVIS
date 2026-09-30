"""The Agent Gateway (docs/29 §11–§13) — Phase 3, **interfaces only** here.

The gateway is the single choke point through which an agent run will reach
models and tools. Phase 1 ships only the pure model-routing rule
(`model_routing.py`) so the model-as-tool path is fixed now: an agent asks for
a *kind* of model, deterministic code picks a permitted configured model tool,
and the call itself is an ordinary `model.invoke` operation authorized by the
engine. No run tokens, no HTTP surface, no MCP transport exist yet.
"""
