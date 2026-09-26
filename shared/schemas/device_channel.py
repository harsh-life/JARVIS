"""The server ↔ Android device channel (docs/23 §4) — the wire contract.

`shared/schemas/` is the only thing `server/` and `android/` share (16 §4,
REPO-T3). The Android client mirrors every type here field for field in
`android/contract/` and both sides parse strictly — `extra="forbid"` here,
`ignoreUnknownKeys = false` there — so a renamed or added field is a parse
failure on the other side rather than a silently dropped value. The shared
conformance vectors (`shared/android/conformance_vectors.json`) exercise
both parsers against the same bytes.

What none of these types can do is carry authority. An operation envelope
names what an already-authorized server decision asks the device to do; it
has no `authorized`, `tier`, `confirmed` or `grant` field, because the device
is not a place where authority is decided (docs/23 §0). A result envelope is
an **untrusted observation** (PRD §24): the server validates its shape, bounds
its size, and hands it to the worker as data, never as instructions.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

# docs/23 §4: "expires_at is short (default 30 s)". The ceiling stops a server
# misconfiguration from minting long-lived device authority; a device refuses
# an envelope whose window exceeds it as malformed.
DEFAULT_OPERATION_TTL = timedelta(seconds=30)
MAX_OPERATION_TTL = timedelta(seconds=60)

# docs/23 §4: results are size-bounded. The hub checks the raw frame length
# before it parses anything, so an oversized frame costs no JSON parsing.
MAX_RESULT_FRAME_BYTES = 64 * 1024
# The one primitive allowed to return image bytes (base64, docs/23 §6 level 4).
MAX_SCREENSHOT_FRAME_BYTES = 2 * 1024 * 1024
# Control frames (hello, cancel, platform status) are tiny.
MAX_CONTROL_FRAME_BYTES = 8 * 1024

# Perception bounds (docs/23 §6: "bounded node list", never an unbounded tree).
MAX_SCREEN_NODES = 300
MAX_NODE_TEXT = 500
MAX_OCR_BLOCKS = 200
MAX_OCR_TEXT = 1000
MAX_NOTIFICATIONS = 50

PACKAGE_NAME_PATTERN = r"^[A-Za-z][A-Za-z0-9_]*(\.[A-Za-z][A-Za-z0-9_]*)+$"


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ── vocabularies ────────────────────────────────────────────────────────


class DeviceMechanism(str, Enum):
    """How a primitive runs on the device (08 §1). Accessibility and concrete
    Android APIs are the defaults; Shizuku only where a primitive genuinely
    needs it, and only as a typed primitive — never a shell (08 §6)."""

    ACCESSIBILITY = "accessibility"
    ANDROID_API = "android_api"
    SHIZUKU = "shizuku"


class DevicePlatformDependency(str, Enum):
    """A device-side dependency an operation can need. Reported with a
    `platform_unavailable` refusal so the user is told exactly what to enable
    — and so the server can hold the *task* (never the operation) until it is
    back (on-demand dependencies, e.g. Shizuku after a reboot)."""

    ACCESSIBILITY_SERVICE = "accessibility_service"
    SHIZUKU = "shizuku"
    NOTIFICATION_ACCESS = "notification_access"
    SCREEN_CAPTURE = "screen_capture"
    OCR = "ocr"


class GridToggle(str, Enum):
    """The per-app grid column (PRD §13) that governs an operation on the
    device. The grid can only refuse; it never grants (docs/23 §5.2)."""

    SCREEN_READ = "screen_read"
    UI_INTERACTION = "ui_interaction"
    SCREENSHOT = "screenshot"
    DEVICE_STATE = "device_state"


class PerceptionLevel(str, Enum):
    """docs/23 §6 — how the screen was read, reported on every perception
    result so the server and the user can see which rung was used."""

    ACCESSIBILITY = "accessibility"
    APP_METADATA = "app_metadata"
    OCR = "ocr"
    VISION = "vision"


class DeviceResultStatus(str, Enum):
    OK = "ok"
    REFUSED = "refused"
    FAILED = "failed"


class DeviceRefusalReason(str, Enum):
    """Why the device-side guard (08 §4, docs/23 §5.2) refused. Every value
    only *narrows*: there is no reason that lets an operation proceed."""

    WRONG_DEVICE = "wrong_device"
    OPERATION_EXPIRED = "operation_expired"
    MAPPING_VERSION_MISMATCH = "mapping_version_mismatch"
    NOT_IN_MAPPING = "not_in_mapping"
    TOGGLE_OFF = "toggle_off"
    MALFORMED_ARGUMENTS = "malformed_arguments"
    PACKAGE_MISMATCH = "package_mismatch"
    SENSITIVE_PACKAGE = "sensitive_package"
    SECURE_WINDOW = "secure_window"
    PLATFORM_UNAVAILABLE = "platform_unavailable"
    CANCELLED = "cancelled"
    REVOKED = "revoked"
    DUPLICATE_OPERATION = "duplicate_operation"


class DeviceFailureReason(str, Enum):
    """The operation was allowed and attempted, and did not work (08 §7: an
    absent target fails as an observation — the device never taps blindly)."""

    TARGET_NOT_FOUND = "target_not_found"
    ACTION_FAILED = "action_failed"
    TIMEOUT = "timeout"
    INTERNAL = "internal"


# ── server → device ─────────────────────────────────────────────────────


class DeviceOperationEnvelope(_Strict):
    """docs/23 §4's envelope, plus `mapping_version` (§5.1: the device
    executes only a triple present in the table *of the same version the
    server used*)."""

    type: Literal["operation"] = "operation"
    op_id: UUID
    task_id: UUID
    device_id: UUID
    capability: str = Field(min_length=1, max_length=64)
    operation: str = Field(min_length=1, max_length=64)
    primitive: str = Field(min_length=1, max_length=96)
    package_name: str | None = Field(default=None, max_length=255, pattern=PACKAGE_NAME_PATTERN)
    arguments: dict[str, Any] = Field(default_factory=dict)
    mapping_version: str = Field(min_length=1, max_length=64)
    issued_at: datetime
    expires_at: datetime

    @model_validator(mode="after")
    def _bounded_window(self) -> "DeviceOperationEnvelope":
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        if self.expires_at - self.issued_at > MAX_OPERATION_TTL:
            raise ValueError("operation window exceeds the maximum operation TTL")
        return self


class DeviceCancel(_Strict):
    """docs/23 §4: `{cancel: op_id | task_id}` — exactly one."""

    type: Literal["cancel"] = "cancel"
    op_id: UUID | None = None
    task_id: UUID | None = None

    @model_validator(mode="after")
    def _exactly_one(self) -> "DeviceCancel":
        if (self.op_id is None) == (self.task_id is None):
            raise ValueError("a cancel names exactly one of op_id or task_id")
        return self


class DeviceHelloAck(_Strict):
    type: Literal["hello_ok"] = "hello_ok"
    device_id: UUID
    mapping_version: str
    server_time: datetime
    # Until this instant the socket's access token is valid; the device sends
    # `reauth` with a fresh token before it, or the server closes the socket.
    session_expires_at: datetime


class DeviceWakePush(_Strict):
    """The entire push payload (docs/23 §4, ANDC-T9): "reconnect", nothing
    else. No operation, no user or screen content, no identifiers beyond what
    the push provider itself needs to route it."""

    type: Literal["wake"] = "wake"


# ── device → server ─────────────────────────────────────────────────────


class DeviceHello(_Strict):
    """First frame on a new socket. Credentials travel in the frame, never in
    the URL, so they cannot land in an access log (SECRET-004)."""

    type: Literal["hello"] = "hello"
    access_token: str = Field(min_length=1, max_length=512)
    device_proof: str = Field(min_length=1, max_length=512)
    mapping_version: str = Field(min_length=1, max_length=64)
    client_version: str = Field(min_length=1, max_length=64)


class DeviceReauth(_Strict):
    type: Literal["reauth"] = "reauth"
    access_token: str = Field(min_length=1, max_length=512)


class DevicePlatformStatus(_Strict):
    """Availability of on-demand device dependencies. Informational only —
    it can let a *waiting task* be re-evaluated server-side, never make an
    operation authorized (docs/23 §5.3, on-demand Shizuku)."""

    type: Literal["platform_status"] = "platform_status"
    platforms: dict[DevicePlatformDependency, bool]


class DeviceResultEnvelope(_Strict):
    """docs/23 §4's result envelope. Untrusted data (PRD §24)."""

    type: Literal["result"] = "result"
    op_id: UUID
    status: DeviceResultStatus
    refusal_reason: DeviceRefusalReason | None = None
    failure_reason: DeviceFailureReason | None = None
    required_platform: DevicePlatformDependency | None = None
    result: dict[str, Any] | None = None
    perception_level: PerceptionLevel | None = None

    @model_validator(mode="after")
    def _consistent(self) -> "DeviceResultEnvelope":
        if self.status is DeviceResultStatus.REFUSED:
            if self.refusal_reason is None or self.failure_reason is not None:
                raise ValueError("a refused result carries a refusal_reason and nothing else")
            if self.result is not None or self.perception_level is not None:
                raise ValueError("a refused result carries no result")
        elif self.status is DeviceResultStatus.FAILED:
            if self.failure_reason is None or self.refusal_reason is not None:
                raise ValueError("a failed result carries a failure_reason and nothing else")
            if self.result is not None:
                raise ValueError("a failed result carries no result")
        else:
            if self.refusal_reason is not None or self.failure_reason is not None:
                raise ValueError("an ok result carries no refusal or failure reason")
        platform_refusal = self.refusal_reason is DeviceRefusalReason.PLATFORM_UNAVAILABLE
        if platform_refusal != (self.required_platform is not None):
            raise ValueError("required_platform is set exactly for platform_unavailable")
        return self


# ── typed perception results (validated server-side; untrusted) ─────────


class ScreenNode(_Strict):
    """One Accessibility node (docs/23 §6 level 1). `text` is always absent
    for a password node — the device redacts before serializing, and the
    server rejects a password node that carries any text (ANDC-T6)."""

    id: int = Field(ge=0, lt=MAX_SCREEN_NODES)
    parent: int | None = Field(default=None, ge=0, lt=MAX_SCREEN_NODES)
    role: str = Field(max_length=64)
    text: str | None = Field(default=None, max_length=MAX_NODE_TEXT)
    content_description: str | None = Field(default=None, max_length=MAX_NODE_TEXT)
    view_id: str | None = Field(default=None, max_length=200)
    bounds: tuple[int, int, int, int]
    clickable: bool = False
    editable: bool = False
    scrollable: bool = False
    enabled: bool = True
    checked: bool | None = None
    selected: bool | None = None
    password: bool = False

    @model_validator(mode="after")
    def _password_redacted(self) -> "ScreenNode":
        if self.password and (self.text is not None or self.content_description is not None):
            raise ValueError("password nodes must be redacted on the device")
        return self


class AppMetadata(_Strict):
    """docs/23 §6 level 2."""

    package_name: str = Field(max_length=255, pattern=PACKAGE_NAME_PATTERN)
    activity: str | None = Field(default=None, max_length=255)
    window_title: str | None = Field(default=None, max_length=MAX_NODE_TEXT)


class OcrBlock(_Strict):
    text: str = Field(max_length=MAX_OCR_TEXT)
    bounds: tuple[int, int, int, int]
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class ScreenReadResult(_Strict):
    app: AppMetadata
    nodes: list[ScreenNode] = Field(default_factory=list, max_length=MAX_SCREEN_NODES)
    ocr_blocks: list[OcrBlock] = Field(default_factory=list, max_length=MAX_OCR_BLOCKS)
    truncated: bool = False


class NotificationItem(_Strict):
    package_name: str = Field(max_length=255, pattern=PACKAGE_NAME_PATTERN)
    title: str | None = Field(default=None, max_length=MAX_NODE_TEXT)
    text: str | None = Field(default=None, max_length=MAX_NODE_TEXT)
    posted_at: datetime


class ScreenshotResult(_Strict):
    """docs/23 §6 level 4. Transient: the server hands the bytes to the vision
    model and drops them; they are never persisted, logged, or written to
    memory (ANDC-T7)."""

    app: AppMetadata
    image_webp_base64: str = Field(min_length=1, max_length=MAX_SCREENSHOT_FRAME_BYTES)
    width: int = Field(gt=0, le=10_000)
    height: int = Field(gt=0, le=10_000)


# WebSocket close codes (4000–4999 are application-defined, RFC 6455 §7.4.2).
class DeviceCloseCode(int, Enum):
    AUTH_FAILED = 4001
    AUTH_EXPIRED = 4002
    REVOKED = 4003
    MAPPING_VERSION_MISMATCH = 4004
    SUPERSEDED = 4005
    PROTOCOL_ERROR = 4008
    CHANNEL_DISABLED = 4009


__all__ = [
    "DEFAULT_OPERATION_TTL",
    "MAX_CONTROL_FRAME_BYTES",
    "MAX_NOTIFICATIONS",
    "MAX_OCR_BLOCKS",
    "MAX_OPERATION_TTL",
    "MAX_RESULT_FRAME_BYTES",
    "MAX_SCREENSHOT_FRAME_BYTES",
    "MAX_SCREEN_NODES",
    "PACKAGE_NAME_PATTERN",
    "AppMetadata",
    "DeviceCancel",
    "DeviceCloseCode",
    "DeviceFailureReason",
    "DeviceHello",
    "DeviceHelloAck",
    "DeviceMechanism",
    "DeviceOperationEnvelope",
    "DevicePlatformDependency",
    "DevicePlatformStatus",
    "DeviceReauth",
    "DeviceRefusalReason",
    "DeviceResultEnvelope",
    "DeviceResultStatus",
    "DeviceWakePush",
    "GridToggle",
    "NotificationItem",
    "OcrBlock",
    "PerceptionLevel",
    "ScreenNode",
    "ScreenReadResult",
    "ScreenshotResult",
]
