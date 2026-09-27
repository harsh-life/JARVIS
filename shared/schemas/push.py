"""Push wake (docs/23 §4, docs/22 §3) — the one content-free signal.

A push tells a sleeping phone exactly one thing: *reconnect*. It carries no
operation, no task, no user content, no identifier and no authorization data.
Everything substantive travels over JARVIS's own authenticated device channel
once the phone is back (ANDC-T9, SCH-T4).

This module is the **only** place the push payload is defined. The server's
sender builds every message with `fcm_wake_message` and nothing else; the
Android client accepts a data map only if it equals `WAKE_DATA` exactly. The
shape is exported to `shared/android/push_samples.json` and held there by a
drift test, so adding a field here is a visible, reviewed change on both
sides — never a quiet extra key.

The provider-side metadata in the FCM message (priority, time-to-live,
collapse key) is delivery routing, not content: fixed constants, identical for
every device, user and task.
"""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# The complete data a wake carries.
WAKE_TYPE = "wake"
WAKE_DATA: dict[str, str] = {"type": WAKE_TYPE}

# A wake older than the longest a task waits for its device (the runtime's
# `max_platform_wait_seconds` default) is useless; FCM drops it after this.
WAKE_TTL_SECONDS = 600
# Undelivered wakes to one device collapse into one.
WAKE_COLLAPSE_KEY = "jarvis_wake"

# An FCM registration token: an opaque, URL-safe string (typically ~160
# characters). Bounded and charset-checked so the field can carry nothing else.
FCM_TOKEN_PATTERN = r"^[A-Za-z0-9_:\-]{32,4096}$"


class PushProvider(str, Enum):
    NONE = "none"
    FCM = "fcm"


class WakeData(BaseModel):
    """The data map of every push this server sends: `{"type": "wake"}`."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    type: Literal["wake"] = WAKE_TYPE


def fcm_wake_message(token: str) -> dict:
    """The complete FCM HTTP v1 request body for a wake to one registration
    token. A *data* message (no `notification` block, so the system never
    renders text from it) at high priority, so a dozing phone may start its
    foreground service to reconnect."""

    return {
        "message": {
            "token": token,
            "data": WakeData().model_dump(),
            "android": {
                "priority": "HIGH",
                "ttl": f"{WAKE_TTL_SECONDS}s",
                "collapse_key": WAKE_COLLAPSE_KEY,
            },
        }
    }


class PushTokenRegistration(BaseModel):
    """`PUT /devices/me/push-token` — the device's own registration token.

    No device, user or graph id: the device is the authenticated caller
    (PHONE-003); the server binds the token to exactly that device."""

    model_config = ConfigDict(extra="forbid", strict=True)

    provider: Literal["fcm"]
    token: str = Field(pattern=FCM_TOKEN_PATTERN)


class FcmClientOptions(BaseModel):
    """The Firebase *client* identifiers a phone needs to obtain a registration
    token — public identifiers (they ship inside any FCM app), never the
    server's sending credential."""

    model_config = ConfigDict(extra="forbid", strict=True)

    project_id: str = Field(pattern=r"^[a-z][a-z0-9\-]{4,61}[a-z0-9]$")
    application_id: str = Field(pattern=r"^1:[0-9]{6,20}:android:[0-9a-f]{8,64}$")
    api_key: str = Field(pattern=r"^[A-Za-z0-9_\-]{20,80}$")
    sender_id: str = Field(pattern=r"^[0-9]{6,20}$")


class PushClientConfig(BaseModel):
    """`GET /devices/push-config`: whether this server can wake phones, and
    how. `provider: none` (the default) means the phone never initializes a
    push SDK at all."""

    model_config = ConfigDict(extra="forbid")

    provider: PushProvider
    fcm: FcmClientOptions | None = None
