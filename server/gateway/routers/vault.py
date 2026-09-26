"""Knowledge Vault endpoints — 02 §8, `11` §7, docs/21 §5.

`GET /api/v1/vault/query` is a RAG query over static, shared, curated knowledge
(VAULT-002). It never touches the memory store (VAULT-003): the vault has its
own client, directory and collection, and this router reaches only the vault
port. Results carry no visibility triplet — vault content is shared by
construction and grants no credentials or authorization (VAULT-004).

`POST /api/v1/vault/documents` is deliberately **not** implemented: in the pilot,
vault content changes only through Git review and `python -m server.vault
reindex` (OD-VLT-1, MP-T10). There is no route to open.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from server.gateway.deps import get_db_session, get_principal
from server.gateway.errors import AppError
from server.gateway.memory_port import VaultPort
from shared.schemas.authorization import Principal
from shared.schemas.errors import ErrorCode
from shared.schemas.memory import VaultQueryRequest, VaultQueryResponse

router = APIRouter(tags=["vault"])


def _vault(request: Request) -> VaultPort:
    port = getattr(request.app.state, "vault", None)
    if port is None:
        raise AppError(
            ErrorCode.DEPENDENCY_UNAVAILABLE,
            "the knowledge vault is not available on this server",
            details={"dependency": "vault"},
        )
    return port


@router.get("/vault/query", response_model=VaultQueryResponse)
async def vault_query(
    request: Request,
    domain: str = Query(min_length=1, max_length=63),
    question: str = Query(min_length=1, max_length=1000),
    top_k: int = Query(default=5, gt=0, le=50),
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
) -> VaultQueryResponse:
    return await _vault(request).query(
        session, principal=principal,
        request=VaultQueryRequest(domain=domain, question=question, top_k=top_k),
    )


@router.get("/vault/status")
async def vault_status(request: Request, principal: Principal = Depends(get_principal)) -> dict:
    port = getattr(request.app.state, "vault", None)
    if port is None:
        return {"enabled": False, "available": False}
    return await port.status()
