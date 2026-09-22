"""vault — placeholder package (foundation branch).

Not implemented in the `foundation` branch. This package exists only so that:
  1. the repository layout matches 00_CANONICAL_PRD.md §45 / 16_REPOSITORY_MODULE_BOUNDARIES.md §1, and
  2. module-boundary (import-linter) contracts about this package are meaningful
     for later branches (e.g. "server.agent must never import server.secrets").

Do not add implementation logic here from the `foundation` branch. The subsystem
document that owns this package's real implementation is named below.

Owning subsystem doc: 11_MEMORY_CONTEXT_VISIBILITY.md
"""
