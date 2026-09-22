"""The execution branch — the constrained-execution boundary consumed by tools.

Layering (16 §2, encoded in `pyproject.toml`'s `[tool.importlinter]`):

    tools / modeltools           (application — builds an ExecutionRequest,
                                   calls a primitive, translates the result)
        |
    execution                    (this package — process primitives + the
                                   typed contracts every primitive returns)
        |
    net | fs                     (destination/path containment)
        |
    security                     (audit)

`server/execution` is deliberately thin. It does not decide *whether* an
operation is authorized — that already happened in `04`/`07` before a
`ToolInvocation` reaches a tool adapter at all (07 §8's flow diagram). It
decides how the *already-authorized* operation is carried out without
letting a tool (or a compromised one) reach anything the operation's own
contract didn't declare. See `contracts.py` for the boundary types.
"""

from __future__ import annotations
