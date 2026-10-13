"""02_API_PROTOCOL.md §12: `GET /api/v1/health` — public liveness, no
data, no auth — and `GET /api/v1/ready` — public readiness (pilot plan
F10/C3, register §2N).

`/health` is unchanged: it reports process liveness and DB reachability and
**always returns 200** — the liveness probe must not become a side channel
for probing internal state through status codes.

`/ready` is the deliberate exception, for a process manager, a reverse proxy
or a monitor: `200` only when every component needed to serve a request is in
place, else `503`. It carries component booleans and nothing else — no
versions, no revision ids, no error text:

* `database` — the store answers;
* `migrations` — the store's schema is at the code's Alembic head (an
  un-migrated or half-migrated store is not ready; 15 §3 makes migration a
  bootstrap step, never a startup side effect);
* `secrets` — the SecretStore is unlocked with the KEK (12 §3/§8: the server
  never serves in a secretless mode).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

import server.storage
from server.gateway.deps import get_db_session

router = APIRouter(tags=["health"])

_MIGRATIONS = Path(server.storage.__file__).resolve().parent / "migrations"


@router.get("/health")
async def health(session: AsyncSession = Depends(get_db_session)) -> dict:
    db_ok = True
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        db_ok = False

    return {"status": "ok" if db_ok else "degraded", "database": "ok" if db_ok else "unreachable"}


@lru_cache(maxsize=1)
def _code_heads() -> frozenset[str]:
    from alembic.script import ScriptDirectory

    return frozenset(ScriptDirectory(str(_MIGRATIONS)).get_heads())


async def _store_revisions(session: AsyncSession) -> frozenset[str] | None:
    # In a savepoint, so a missing table (an un-migrated store) cannot leave
    # the request's transaction aborted on PostgreSQL.
    try:
        async with session.begin_nested():
            rows = (await session.execute(text("SELECT version_num FROM alembic_version"))).scalars().all()
    except Exception:
        return None
    return frozenset(rows)


@router.get("/ready")
async def ready(request: Request, session: AsyncSession = Depends(get_db_session)) -> JSONResponse:
    database = True
    try:
        await session.execute(text("SELECT 1"))
    except Exception:
        database = False
    revisions = await _store_revisions(session) if database else None
    try:
        migrations = revisions is not None and revisions == _code_heads()
    except Exception:
        migrations = False
    security = getattr(request.app.state, "security", None)
    secrets = bool(security is not None and security.secret_store.is_unlocked)
    body = {
        "ready": database and migrations and secrets,
        "database": database,
        "migrations": migrations,
        "secrets": secrets,
    }
    return JSONResponse(body, status_code=200 if body["ready"] else 503, headers={"Cache-Control": "no-store"})
