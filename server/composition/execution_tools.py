"""Builds the execution branch's tools from `ExecutionConfig` — the seam
`build_application` (server/composition/__init__.py) uses by default, and
that a self-hoster's own entrypoint or a test can override via
`extra_tools` exactly as before this branch existed.

Every primitive here is constructed once per process and closed over by its
adapter (server/tools/platforms.py); nothing is re-read from config per
call, so a request can never widen what it was configured with.
"""

from __future__ import annotations

from server.config.schema import AppConfig
from server.execution.process import ConstrainedProcessExecutor
from server.fs import FilesystemSandbox
from server.net import EgressClient
from server.tools.platforms import (
    android_app_interact_tool,
    android_device_read_tool,
    file_read_tool,
    file_write_tool,
    net_request_tool,
    shell_command_tool,
)
from server.tools.registry import ToolDefinition
from shared.schemas.execution import EgressPolicy


def build_execution_tools(config: AppConfig) -> list[ToolDefinition]:
    """`file.read`/`file.write` are always real and immediately usable
    (sandboxed, quota-capped) once a principal is granted the capability —
    there is nothing unsafe about that by default (09's containment holds
    regardless of what an operator has or hasn't configured). `net.request`
    and `system.shell` are registered too, so the capability/tool machinery
    is provably wired end-to-end, but are inert by default (10/§4's
    "closed by default" posture): an empty destination list and an empty
    executable allow-list mean granting the capability still authorizes
    nothing to actually happen until an operator configures one. The
    Android tools use `UnavailableDeviceTransport` (no `android/` client
    exists yet in this repository) and fail every call deterministically.
    """

    fs_config = config.execution.filesystem
    sandbox = FilesystemSandbox(
        base_root=fs_config.base_root,
        containment_mode=fs_config.containment_mode,
        max_file_bytes=fs_config.max_file_bytes,
        max_sandbox_bytes=fs_config.max_sandbox_bytes,
        max_archive_entries=fs_config.max_archive_entries,
        max_archive_uncompressed_bytes=fs_config.max_archive_uncompressed_bytes,
    )

    net_config = config.execution.network
    egress_client = EgressClient(enforcement_mode=net_config.enforcement_mode)
    default_policy = EgressPolicy(
        destinations=frozenset(net_config.default_destinations),
        internet=net_config.default_internet,
        private_net=net_config.default_private_net,
        may_send_credentials=False,
        max_response_bytes=net_config.max_response_bytes,
        connect_timeout_seconds=net_config.connect_timeout_seconds,
        read_timeout_seconds=net_config.read_timeout_seconds,
        max_redirects=net_config.max_redirects,
    )

    process_config = config.execution.process
    executor = ConstrainedProcessExecutor(
        allowed_executables=process_config.allowed_executables,
        default_timeout_seconds=process_config.default_timeout_seconds,
        max_timeout_seconds=process_config.max_timeout_seconds,
        max_output_bytes=process_config.max_output_bytes,
        confinement_mode=process_config.confinement_mode,
        read_only_paths=process_config.read_only_paths,
    )

    return [
        file_read_tool(sandbox),
        file_write_tool(sandbox),
        net_request_tool(egress_client, egress_policy=default_policy),
        shell_command_tool(executor, sandbox=sandbox),
        android_app_interact_tool(),
        android_device_read_tool(),
    ]


__all__ = ["build_execution_tools"]
