"""The sensitive-app gate (docs/CAPABILITY_MATRIX.md §5.1, docs/23 §5.5).

A single `tap` on a payment app's "Pay" button *is* the consequential act, so
a UI primitive's registry tier cannot be trusted on its own once it runs
inside someone else's app. docs/23 §5.5: until the owner ratifies a
sensitive-app classification, the Android build ships perception and
`device.read` first, and enables UI control only for packages the owner has
explicitly classified.

What this module implements, deterministically and keyed only on the
operation's `resource_scope` (never model-judged):

| Package class | UI-acting operations | `capture_screenshot` |
|---|---|---|
| unclassified (the default for every app) | **denied** — never confirmable | **denied** |
| `non_sensitive` | the registry tier | allowed (its tier) |
| `sensitive` | at least `consequential` | **denied** |
| `payment` | `high_irreversible` (confirmation + step-up) | **denied** |

"Not classified" is never treated as permission. Reads (`read_screen`,
`read_screen_element`, `read_notification`, `read_battery`) are untouched.

Which operations are "UI-acting" and which is the screenshot is not a second
hand-kept list: it is the shared device mapping's own grid toggle
(`server/execution/android.py`, docs/23 §5.1), so the gate and the device's
per-app grid can never disagree about what an operation is.

The lists are operator configuration (`android.app_classification`) — the
owner's ratified classification is data, not a code change.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

from server.execution.android import DEVICE_MAPPING
from shared.schemas.device_channel import GridToggle
from shared.schemas.enums import RiskCategory


class AppClass(str, Enum):
    NON_SENSITIVE = "non_sensitive"
    SENSITIVE = "sensitive"
    PAYMENT = "payment"


# Denial reasons (the engine's `reason`, surfaced as 403).
APP_NOT_CLASSIFIED = "app_not_classified"
SCREENSHOT_NOT_PERMITTED = "screenshot_not_permitted_for_app"

_UI_ACTING: frozenset[tuple[str, str]] = frozenset(
    (capability, operation)
    for capability, operations in DEVICE_MAPPING.items()
    for operation, spec in operations.items()
    if spec.grid_toggle is GridToggle.UI_INTERACTION
)
_SCREENSHOT: frozenset[tuple[str, str]] = frozenset(
    (capability, operation)
    for capability, operations in DEVICE_MAPPING.items()
    for operation, spec in operations.items()
    if spec.grid_toggle is GridToggle.SCREENSHOT
)

_MINIMUM_TIER: dict[AppClass, RiskCategory | None] = {
    AppClass.NON_SENSITIVE: None,
    AppClass.SENSITIVE: RiskCategory.CONSEQUENTIAL,
    AppClass.PAYMENT: RiskCategory.HIGH_IRREVERSIBLE,
}


@dataclass(frozen=True)
class ScopeConstraint:
    """What the classification says about one operation: refuse it outright
    (`denial`), or raise its tier to at least `minimum_tier`."""

    denial: str | None = None
    minimum_tier: RiskCategory | None = None


@dataclass(frozen=True)
class AppClassification:
    non_sensitive: frozenset[str] = field(default_factory=frozenset)
    sensitive: frozenset[str] = field(default_factory=frozenset)
    payment: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        overlap = (
            (self.non_sensitive & self.sensitive)
            | (self.non_sensitive & self.payment)
            | (self.sensitive & self.payment)
        )
        if overlap:
            raise ValueError(f"packages classified twice: {sorted(overlap)}")

    def classify(self, package_name: str | None) -> AppClass | None:
        if not package_name:
            return None
        if package_name in self.payment:
            return AppClass.PAYMENT
        if package_name in self.sensitive:
            return AppClass.SENSITIVE
        if package_name in self.non_sensitive:
            return AppClass.NON_SENSITIVE
        return None

    def constraint(
        self,
        *,
        capability: str | None,
        capability_operation: str | None,
        resource_scope: Mapping[str, str] | None,
    ) -> ScopeConstraint:
        key = (capability or "", capability_operation or "")
        if key not in _UI_ACTING and key not in _SCREENSHOT:
            return ScopeConstraint()
        app_class = self.classify((resource_scope or {}).get("package_name"))
        if key in _SCREENSHOT:
            if app_class is AppClass.NON_SENSITIVE:
                return ScopeConstraint()
            return ScopeConstraint(denial=SCREENSHOT_NOT_PERMITTED)
        if app_class is None:
            return ScopeConstraint(denial=APP_NOT_CLASSIFIED)
        return ScopeConstraint(minimum_tier=_MINIMUM_TIER[app_class])

    def public_view(self) -> dict[str, list[str]]:
        """What a device may cache to refuse early (docs/23 §5.2 — the device
        guard only narrows). Package names of apps, not secrets."""

        return {
            "non_sensitive": sorted(self.non_sensitive),
            "sensitive": sorted(self.sensitive),
            "payment": sorted(self.payment),
        }


__all__ = [
    "APP_NOT_CLASSIFIED",
    "SCREENSHOT_NOT_PERMITTED",
    "AppClass",
    "AppClassification",
    "ScopeConstraint",
]
