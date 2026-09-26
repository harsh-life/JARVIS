"""The sensitive-app gate (docs/CAPABILITY_MATRIX.md §5.1, docs/23 §5.5).

End to end through the real runtime and engine: an unclassified app gets no UI
control at all (not even a confirmation prompt), a sensitive app's UI
operations need confirmation, a payment app's need confirmation *and*
step-up, a screenshot is possible only in an app classified non-sensitive, and
reading is never affected. The harness classifies `com.example` as
non-sensitive; the packages below are chosen per case.
"""

from __future__ import annotations

import pytest

from server.capabilities.app_classification import (
    APP_NOT_CLASSIFIED,
    SCREENSHOT_NOT_PERMITTED,
    AppClassification,
)
from server.capabilities.policy import RiskPolicyAdapter
from server.storage.models import PermissionDecision as PermissionDecisionRow
from shared.schemas.authorization import Operation, ResourceType
from shared.schemas.enums import RiskCategory
from tests.runtime.conftest import ask, call, final, pending_of


UNCLASSIFIED = {"package_name": "org.unknown.app"}
BANK = {"package_name": "com.bank.app"}
WALLET = {"package_name": "com.wallet.pay"}
CLASSIFICATION = {
    "android": {
        "app_classification": {
            "non_sensitive": ["com.example"],
            "sensitive": ["com.bank.app"],
            "payment": ["com.wallet.pay"],
        }
    }
}


def ui(operation: str, **args) -> str:
    return call("ui.app", operation, args=args or {"id": "x"}, platform="android")


# ── policy level ────────────────────────────────────────────────────────


def _tier(policy, capability, operation, scope):
    return policy.risk_tier(
        resource_type=ResourceType.TOOL_ACTION, operation=Operation.CREATE,
        capability_name=capability, capability_operation=operation, resource_scope=scope,
    )


def _denial(policy, capability, operation, scope):
    return policy.scope_denial(capability_name=capability, capability_operation=operation, resource_scope=scope)


def test_nothing_is_classified_by_default_and_ui_control_is_denied():
    policy = RiskPolicyAdapter()
    for capability, operation in [
        ("app.interact", "tap"), ("app.interact", "swipe"), ("app.interact", "input_text"),
        ("app.interact", "launch_activity"), ("app.interact", "force_stop"),
        ("device.ui_control", "tap"), ("device.ui_control", "global_action"),
    ]:
        assert _denial(policy, capability, operation, {"package_name": "com.example"}) == APP_NOT_CLASSIFIED
    assert _denial(policy, "device.read", "capture_screenshot", {"package_name": "com.example"}) == (
        SCREENSHOT_NOT_PERMITTED
    )


@pytest.mark.parametrize(
    "capability, operation",
    [("app.interact", "read_screen_element"), ("device.read", "read_screen"),
     ("device.read", "read_notification"), ("device.read", "read_battery")],
)
def test_reading_is_never_gated(capability, operation):
    policy = RiskPolicyAdapter()
    assert _denial(policy, capability, operation, UNCLASSIFIED) is None
    assert _tier(policy, capability, operation, UNCLASSIFIED) is RiskCategory.LOW_READ


def test_classification_only_ever_raises_a_tier():
    policy = RiskPolicyAdapter(
        AppClassification(
            non_sensitive=frozenset({"com.example"}),
            sensitive=frozenset({"com.bank.app"}),
            payment=frozenset({"com.wallet.pay"}),
        )
    )
    assert _tier(policy, "app.interact", "tap", {"package_name": "com.example"}) is RiskCategory.LOW_WRITE
    assert _tier(policy, "app.interact", "tap", BANK) is RiskCategory.CONSEQUENTIAL
    assert _tier(policy, "app.interact", "tap", WALLET) is RiskCategory.HIGH_IRREVERSIBLE
    # Already consequential stays consequential, never lowered.
    assert _tier(policy, "app.interact", "input_text", {"package_name": "com.example"}) is RiskCategory.CONSEQUENTIAL
    assert _denial(policy, "device.read", "capture_screenshot", BANK) == SCREENSHOT_NOT_PERMITTED
    assert _denial(policy, "device.read", "capture_screenshot", {"package_name": "com.example"}) is None


def test_a_package_cannot_be_classified_twice():
    with pytest.raises(ValueError):
        AppClassification(non_sensitive=frozenset({"com.x"}), payment=frozenset({"com.x"}))


def test_the_operator_config_is_validated():
    from server.config.schema import AndroidAppClassificationConfig

    with pytest.raises(ValueError):
        AndroidAppClassificationConfig(non_sensitive=["not a package"])
    with pytest.raises(ValueError):
        AndroidAppClassificationConfig(sensitive=["com.x.y"], payment=["com.x.y"])


# ── end to end through the runtime ──────────────────────────────────────


async def test_ui_control_in_an_unclassified_app_is_refused_not_offered(make_harness):
    h = await make_harness(config=CLASSIFICATION)
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=UNCLASSIFIED)
    h.model.push(ask("app.interact", scope=UNCLASSIFIED), ui("tap"), final("gave up"))
    resp = await h.submit(alice)
    assert resp.status_code == 200, resp.text
    assert resp.json()["pending"] is None
    assert h.ui.calls == []
    # The worker sees only the uniform denial (anti-enumeration); the reason is
    # in the audit trail.
    assert "Not found or not permitted." in h.model.all_text()
    decisions = await h.rows(PermissionDecisionRow)
    assert any(d.reason == APP_NOT_CLASSIFIED and d.decision == "deny" for d in decisions)


async def test_reading_an_unclassified_app_still_works(make_harness):
    h = await make_harness(config=CLASSIFICATION)
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=UNCLASSIFIED)
    h.model.push(ask("app.interact", scope=UNCLASSIFIED), ui("read_screen_element"), final("read it"))
    resp = await h.submit(alice)
    assert resp.status_code == 200, resp.text
    assert [c.operation for c in h.ui.calls] == ["read_screen_element"]


async def test_a_tap_in_a_sensitive_app_needs_confirmation(make_harness):
    h = await make_harness(config=CLASSIFICATION)
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=BANK)
    h.model.push(ask("app.interact", scope=BANK), ui("tap"), final("done"))
    pending = pending_of(await h.submit(alice))
    assert pending["pending"]["risk_category"] == "consequential"
    assert pending["pending"]["requires_step_up"] is False
    assert h.ui.calls == []


async def test_a_tap_in_a_payment_app_needs_confirmation_and_step_up(make_harness):
    h = await make_harness(config=CLASSIFICATION)
    alice = await h.user("alice")
    await h.grant(alice, "app.interact", resource_scope=WALLET)
    h.model.push(ask("app.interact", scope=WALLET), ui("tap"), final("done"))
    pending = pending_of(await h.submit(alice))
    assert pending["pending"]["risk_category"] == "high_irreversible"
    assert pending["pending"]["requires_step_up"] is True
    assert h.ui.calls == []


async def test_a_tap_in_a_non_sensitive_app_runs_at_its_own_tier(make_harness):
    h = await make_harness(config=CLASSIFICATION)
    alice = await h.user("alice")
    scope = {"package_name": "com.example"}
    await h.grant(alice, "app.interact", resource_scope=scope)
    h.model.push(ask("app.interact", scope=scope), ui("tap"), final("done"))
    resp = await h.submit(alice)
    assert resp.status_code == 200, resp.text
    assert [c.operation for c in h.ui.calls] == ["tap"]
