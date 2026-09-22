"""02_API_PROTOCOL.md §12: `GET /api/v1/health` — public liveness, no
data, no auth.

[IMPL] choice, documented: the doc's endpoint catalog lists exactly one
health endpoint, not a separate liveness/readiness pair. Rather than invent
an undocumented second public endpoint, this single endpoint reports both
process liveness and DB reachability (a best-effort "readiness" signal) —
covering "health/readiness primitives where appropriate" (§8 of this
branch's instructions) without expanding the documented API surface.
A DB check failure degrades the reported status but still returns 200:
this endpoint is explicitly public/unauthenticated and must not become a
side channel for probing internal state via status codes.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from server.gateway.deps import get_db_session

router = APIRouter(tags=["health"])


@router.get("/health")
async def health(session: AsyncSession = Depends(get_db_session)) -> dict:
    db_ok = True
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    return {"status": "ok" if db_ok else "degraded", "database": "ok" if db_ok else "unreachable"}
