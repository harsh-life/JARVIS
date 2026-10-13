"""Character presentation settings, server side (pilot plan C10, step M2).

OD-CHAR-1/2/3 (`docs/DECISION_REGISTER.md` §2M) make the character a
presentation the endpoint renders and selects locally. The server's only part
is to keep each user's validated `CharacterPresentationSettings` so every
client of that user renders the same way. It is a store, not a decision-maker:

* the stored document is exactly the shared schema — enumerated, boolean and
  numeric fields, no free text that could reach a prompt (persona text stays
  deferred, OD-CHAR-3);
* a malformed document is refused whole;
* nothing on the server reads it to decide anything. The authority packages
  are forbidden from importing `shared.schemas.presentation` (import-linter
  "Character presentation never reaches authority"), which is why this lives
  in its own package and is mounted by the composition root rather than the
  gateway.
"""
