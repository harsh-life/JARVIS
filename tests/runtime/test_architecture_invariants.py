"""Structural proofs that the runtime cannot become the security authority.

These tests target the same class of property `tests/security_core/
test_fail_closed_and_boundaries.py` already establishes for security-core —
scanning code and Protocol shapes rather than exercising one call path —
because the invariant being proven ("X is impossible") is stronger evidence
than "X did not happen in this test run".
"""

from __future__ import annotations

import inspect
from pathlib import Path

from server.agent.ports import ModelInvoker, ToolDispatcher
from server.config.schema import AppConfig
from tests.security_core.helpers import code_only
from tests.support import make_test_config


def test_intelligence_disabled_by_default():
    """INTEL-003 / INV-18, restated at the config-schema level this branch
    actually reads from: the runtime never assumes an IntelligenceProvider."""

    config = make_test_config()
    assert config.intelligence.enabled is False


def test_decision_provider_is_not_implemented_by_this_branch():
    """26_DECISION_PROVIDER.md is `[FUTURE][PROPOSED]`, ratified by no one.
    This branch's own instructions: "If its interface is required by the
    runtime architecture, integrate it as a proposal/decision aid only" —
    since nothing in 05's locked determinism table requires one, this
    branch adds no DecisionProvider code at all, mirroring security-core's
    own absence test for its own scope."""

    for root in ("server/agent", "server/gateway/runtime.py", "server/tools", "server/modeltools", "server/models", "server/memory"):
        path = Path(root)
        paths = [path] if path.is_file() else sorted(path.rglob("*.py"))
        for p in paths:
            code = code_only(p)
            assert "DecisionProvider" not in code, p
            assert "IntelligenceProvider" not in code, p


def test_model_invoker_protocol_has_no_way_to_execute_a_tool():
    """MP-T3-adjacent, this branch's own instruction: "The model cannot...
    select an unrestricted tool" / "ModelProvider may NOT... directly
    execute tools." Structural, not behavioral: `ModelInvoker`'s entire
    method surface is `invoke` and `health` — there is no `dispatch`,
    `execute`, `call_tool`, or any method that takes a `ToolInvocationRequest`
    or an `AccessRequest`."""

    members = {name for name, _ in inspect.getmembers(ModelInvoker) if not name.startswith("_")}
    assert members == {"invoke", "health"}


def test_tool_dispatcher_protocol_takes_no_model_or_authorization_input():
    """`ToolDispatcher.dispatch` accepts only a `ToolInvocationRequest` —
    there is no parameter through which a raw model message, a proposal, or
    an `AccessRequest` could reach it. Whatever calls `dispatch` (only
    `server.agent.orchestrator`, and only after authorization has already
    returned `allow`) is the one place that decision is made."""

    sig = inspect.signature(ToolDispatcher.dispatch)
    params = [p for name, p in sig.parameters.items() if name not in ("self",)]
    assert [p.name for p in params] == ["request"]


def test_agent_proposal_kind_is_a_closed_enum_the_model_cannot_extend():
    """PERM-005/07 §4's "never model judgment" restated for the proposal
    shape itself: a proposal's `kind` must be one of exactly three values —
    there is no `kind` a model could invent that reaches any code path
    other than parse failure."""

    from shared.schemas.runtime import ProposalKind

    assert {k.value for k in ProposalKind} == {"final_answer", "tool_call", "model_tool_call"}


def test_agent_config_default_is_local_first():
    """06 §2 [LOCKED]: local-first via Ollama is the default and recommended
    primary — never a hidden cloud dependency."""

    config = AppConfig.model_validate(
        {
            "security": {"oidc": {"client_id": "x", "issuer": "https://issuer.example"}},
            "secrets": {"kek_source": "env:X"},
        }
    )
    assert config.agent.provider == "ollama"
