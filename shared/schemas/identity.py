"""Identity entities — User, Device, Session.

Source: 01_DATA_MODEL_SCHEMA.md §2. These three stay structurally distinct
per this branch's explicit instruction ("Session is not a User. Device is
not a User."): no shared base class collapses them, even though they share
a couple of field names, because collapsing them would blur the identity
model the rest of the package depends on.

Foundation represents these shapes and their *structural* integrity rules
only (e.g. DM-T6: a Session's user_id must equal its Device's user_id — a
referential-integrity fact, not an authorization decision). It does not
implement OIDC validation, token issuance, or credential lifecycle — that
is 03_AUTH_IDENTITY_SESSION.md's job.
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID, uuid4

from pydantic import Field

from shared.schemas.common import ORMBase, utcnow
from shared.schemas.enums import DevicePlatform, UserStatus


class User(ORMBase):
    """PRD ENT-001, AUTH-004/005."""

    user_id: UUID = Field(default_factory=uuid4)
    oidc_subject: str
    oidc_issuer: str
    display_name: str | None = None
    status: UserStatus = UserStatus.ACTIVE
    created_at: datetime = Field(default_factory=utcnow)


class Device(ORMBase):
    """PRD DEVICE-001, SESSION-002.

    `credential_ref` is a handle into the SecretStore (12_SECRETSTORE.md) —
    never the credential value itself (SECRET-002). Foundation does not
    implement SecretStore resolution; this field is opaque here.
    """

    device_id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    platform: DevicePlatform = DevicePlatform.ANDROID
    credential_ref: str
    registered_at: datetime = Field(default_factory=utcnow)
    last_seen: datetime | None = None
    revoked: bool = False
    revoked_at: datetime | None = None


class Session(ORMBase):
    """PRD SESSION-001.

    `user_id` is denormalized for fast authZ (01 §2.3) but MUST equal the
    owning Device's `user_id` — enforced by `create_session_record()` below,
    not by client assertion (PHONE-003 principle, though PHONE-003 itself
    — deriving identity from a validated token — is 03's job, not this
    schema's).
    """

    session_id: UUID = Field(default_factory=uuid4)
    device_id: UUID
    user_id: UUID
    active_graph_id: UUID | None = None
    issued_at: datetime = Field(default_factory=utcnow)
    expires_at: datetime
    scope: list[str] | None = None


class SessionUserDeviceMismatch(ValueError):
    """Raised when a Session's user_id does not match its Device's user_id.

    DM-T6 (01_DATA_MODEL_SCHEMA.md §15): "a Session whose user_id != Device.user_id
    is rejected." This is a referential-integrity check, not an authorization
    decision — no request/token/capability is being evaluated here.
    """


def new_session_for_device(
    *,
    device: Device,
    expires_at: datetime,
    active_graph_id: UUID | None = None,
    scope: list[str] | None = None,
) -> Session:
    """Construct a Session guaranteed to satisfy DM-T6.

    This is the only foundation-sanctioned way to build a Session from a
    Device, precisely so that "Session.user_id must equal Device.user_id"
    cannot be silently violated by a caller passing mismatched ids by hand.
    """

    return Session(
        device_id=device.device_id,
        user_id=device.user_id,
        active_graph_id=active_graph_id,
        expires_at=expires_at,
        scope=scope,
    )
