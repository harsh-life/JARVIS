"""Character presentation, server side (pilot plan C10; OD-CHAR-4).

Two things live here, and neither decides anything:

* **The settings store** (pilot plan C10): each user's validated
  `CharacterPresentationSettings`, so every client of that user renders the
  same way. The stored document is exactly the shared schema — enumerated,
  boolean and numeric fields, no free text that could reach a prompt (persona
  text stays deferred, OD-CHAR-3); a malformed document is refused whole.
* **The activity scorer** (OD-CHAR-4, `activity.py`): "Central AI"'s
  deterministic reading of the caller's own task rows into per-expression
  scores, computed per request, never stored.

Nothing on the server reads either to decide anything. The authority packages
are forbidden from importing `shared.schemas.presentation` (import-linter
"Character presentation never reaches authority"), and this package may not
import a deciding or acting package ("Presentation settings are a store, not
an authority"), which is why it is mounted by the composition root rather than
the gateway.
"""
