"""Track B server — modular monolith (00_CANONICAL_PRD.md §7, SRV-002).

One FastAPI process, modular routers/packages per subsystem. This top-level
package intentionally contains no logic of its own.
"""
