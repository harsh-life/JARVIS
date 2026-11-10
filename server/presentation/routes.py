"""`GET/PUT/DELETE /api/v1/config/presentation` — the caller's own character
presentation settings (pilot plan C10) — and `GET /api/v1/presentation/activity`,
the caller's own activity report (OD-CHAR-4, `server/presentation/activity.py`).

Owner-scoped by the principal from the bearer token; there is no way to name
another user. `GET` returns the stored settings or the shared defaults; `PUT`
replaces them with a document that must validate as
`CharacterPresentationSettings` in full (strict, no unknown field, bounded
size), or nothing is stored; `DELETE` returns to the defaults. The activity report is computed on each
request from the caller's own task rows and is neither stored nor cached
(`Cache-Control: no-store`).
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Request, Response, status
from pydantic import ValidationError
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from server.gateway.deps import get_db_session, get_principal
from server.gateway.device_scope import DesktopScope, desktop_scope
from server.gateway.errors import AppError
from server.presentation.activity import measure
from server.storage.models import PresentationSettingsRow
from shared.schemas.authorization import Principal
from shared.schemas.errors import ErrorCode
from shared.schemas.presentation import ActivityPresentation, CharacterPresentationSettings

logger = logging.getLogger("hypermind.presentation")

router = APIRouter(tags=["presentation"])

PATH = "/config/presentation"
ACTIVITY_PATH = "/presentation/activity"
# The full document with every field set is well under 2 KB.
MAX_BODY_BYTES = 8_192


async def _stored(session: AsyncSession, principal: Principal) -> CharacterPresentationSettings:
    row = await session.get(PresentationSettingsRow, principal.user_id)
    if row is None:
        return CharacterPresentationSettings()
    try:
        return CharacterPresentationSettings.model_validate_json(json.dumps(row.settings))
    except ValidationError:
        # Written only through PUT's validation, so this is a schema change or
        # tampering: render the defaults rather than anything unvalidated.
        logger.error("stored presentation settings for a user failed validation; serving defaults")
        return CharacterPresentationSettings()


@router.get(PATH, response_model=CharacterPresentationSettings)
async def get_presentation(
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
) -> CharacterPresentationSettings:
    return await _stored(session, principal)


@router.put(PATH, response_model=CharacterPresentationSettings)
async def put_presentation(
    request: Request,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
) -> CharacterPresentationSettings:
    raw = await request.body()
    if len(raw) > MAX_BODY_BYTES:
        raise AppError(ErrorCode.VALIDATION_FAILED, "presentation settings are too large")
    try:
        settings = CharacterPresentationSettings.model_validate_json(raw)
    except ValidationError as exc:
        # Field locations only — never the submitted values.
        fields = sorted({".".join(str(p) for p in error["loc"]) for error in exc.errors()})[:20]
        raise AppError(ErrorCode.VALIDATION_FAILED, "invalid presentation settings",
                       details={"fields": fields}) from None
    row = await session.get(PresentationSettingsRow, principal.user_id)
    document = settings.model_dump(mode="json")
    now = datetime.now(timezone.utc)
    if row is None:
        session.add(PresentationSettingsRow(user_id=principal.user_id, settings=document, updated_at=now))
    else:
        row.settings = document
        row.updated_at = now
    return settings


@router.delete(PATH, status_code=status.HTTP_204_NO_CONTENT)
async def reset_presentation(
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
) -> Response:
    await session.execute(delete(PresentationSettingsRow).where(PresentationSettingsRow.user_id == principal.user_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(ACTIVITY_PATH, response_model=ActivityPresentation)
@desktop_scope(DesktopScope.PRESENTATION)
async def get_activity(
    response: Response,
    session: AsyncSession = Depends(get_db_session),
    principal: Principal = Depends(get_principal),
) -> ActivityPresentation:
    """JARVIS's current operational activity for the caller, as character
    scores (OD-CHAR-4). Presentation only: nothing on the server reads it."""

    response.headers["Cache-Control"] = "no-store"
    return await measure(session, principal.user_id, datetime.now(timezone.utc))
