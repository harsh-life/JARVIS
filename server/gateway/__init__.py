"""FastAPI gateway skeleton (00_CANONICAL_PRD.md §7, SRV-002).

One FastAPI process, modular routers. This branch implements only the
protocol infrastructure 02_API_PROTOCOL.md requires before any real
endpoint can exist: versioning, request_id, the error envelope, DI
scaffolding, and a health endpoint. It implements NO authentication and NO
authorization — every endpoint below `/api/v1` that would need either is
simply absent until the auth/security-core branches add it. There is no
placeholder/fake auth middleware here (§8 of this branch's instructions).
"""
