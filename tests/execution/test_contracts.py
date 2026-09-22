"""server/execution/contracts.py — the execution layer's own typed boundary.

These are narrow unit tests for the contract types themselves: that an
`ExecutionRequest` is a faithful, authority-free copy of an already-authorized
`ToolInvocation`, and that `ResourceScope`/`ExecutionError` fail closed rather
than defaulting. End-to-end proof that the *layer* cannot become a second
authorization system lives in the fs/net/process/platform test suites, which
exercise it against the real `ToolRegistry`/runtime.
"""

from __future__ import annotations

import uuid

import pytest

from server.execution.contracts import (
    ExecutionError,
    ExecutionErrorCode,
    ExecutionRequest,
    ExecutionResult,
    ResourceScope,
)
from shared.schemas.agent import ExecutionPlatform, ToolInvocation


def _invocation(**overrides) -> ToolInvocation:
    payload = dict(
        tool_id="files.read",
        operation="read_file",
        arguments={"relative_path": "notes.txt"},
        user_id=uuid.uuid4(),
        task_id=uuid.uuid4(),
        platform=ExecutionPlatform.SERVER,
        resource_ref=None,
        resource_scope={"sandbox_root": "user-scope"},
    )
    payload.update(overrides)
    return ToolInvocation(**payload)


def test_from_invocation_copies_every_field_faithfully():
    invocation = _invocation()
    request = ExecutionRequest.from_invocation(invocation)

    assert request.tool_id == invocation.tool_id
    assert request.operation == invocation.operation
    assert request.user_id == invocation.user_id
    assert request.task_id == invocation.task_id
    assert request.platform == invocation.platform
    assert dict(request.arguments) == dict(invocation.arguments)
    assert request.resource_ref == invocation.resource_ref
    assert request.scope.raw == dict(invocation.resource_scope)


def test_execution_request_carries_no_authority_fields():
    """The one invariant contracts.py exists to protect: there is no field an
    adapter could set to claim authorization that was never granted."""

    request = ExecutionRequest.from_invocation(_invocation())
    field_names = {f for f in request.__dataclass_fields__}
    assert "authorized" not in field_names
    assert "role" not in field_names
    assert "capability" not in field_names


def test_from_invocation_is_the_only_constructor_path_worth_using():
    # Building one "by hand" is possible (it's a dataclass), but nothing in
    # the type grants it any more authority than the caller supplies — there
    # is no validation step that could be tricked into upgrading a
    # hand-built request. The from_invocation path is what every adapter is
    # written to use (enforced by review/design, not by the type system).
    invocation = _invocation(resource_scope=None)
    request = ExecutionRequest.from_invocation(invocation)
    assert request.scope.raw == {}


def test_resource_scope_require_fails_closed_when_key_missing():
    scope = ResourceScope(raw={})
    with pytest.raises(ExecutionError) as excinfo:
        scope.require("sandbox_root")
    assert excinfo.value.code is ExecutionErrorCode.MISSING_CONTEXT


def test_resource_scope_require_fails_closed_on_empty_string_value():
    # An empty string is not a valid scope value either — treating "" as
    # "unscoped" would be the same widening bug the registry's
    # validate_resource_scope guards against on the grant side.
    scope = ResourceScope(raw={"sandbox_root": ""})
    with pytest.raises(ExecutionError):
        scope.require("sandbox_root")


def test_resource_scope_require_returns_the_value_when_present():
    scope = ResourceScope(raw={"sandbox_root": "user-scope"})
    assert scope.require("sandbox_root") == "user-scope"


def test_resource_scope_get_returns_none_without_raising():
    scope = ResourceScope(raw={})
    assert scope.get("sandbox_root") is None


def test_execution_error_carries_code_and_message():
    error = ExecutionError(ExecutionErrorCode.SANDBOX_VIOLATION, "escaped the sandbox root")
    assert error.code is ExecutionErrorCode.SANDBOX_VIOLATION
    assert "escaped" in error.message
    assert "escaped" in str(error)


def test_execution_result_defaults_are_empty_and_inert():
    result = ExecutionResult()
    assert result.content == ""
    assert result.units == 1
    assert dict(result.metadata) == {}
