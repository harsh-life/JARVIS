"""The Browser Use container's entrypoint (OD-AF-6, P2; docs/29 §21.1).

Untrusted execution infrastructure. It reads the run's task from
`/scratch/task.json` (written by JARVIS), runs Browser Use with:

* its model = the JARVIS Model Gateway, over `/run/jarvis/model.sock`, with
  the run's model token (`jarvis_llm.py`) — no provider key exists here;
* its browser's only network = JARVIS's CONNECT proxy, over
  `/run/jarvis/egress.sock`, reached through a loopback forwarder — the
  container has no network interface, so this is the only way out, and the
  proxy decides every host (OD-AF-13);
* `allowed_domains` = the run's hosts — **advisory only**, a convenience to
  the agent; the proxy is the authority;
* no stored credentials, a fresh profile in scratch, `use_vision` off,
  telemetry and cloud sync off (and unreachable anyway);

and writes `/scratch/result.json` — data, which JARVIS bounds, scrubs and
delivers to the owner's inbox. Nothing here can confirm, grant, authorize or
send anything anywhere else.
"""

from __future__ import annotations

import asyncio
import os
import sys

# The only way out is the forwarder below, to JARVIS's proxy; the browser is
# told so explicitly. An inherited proxy setting (an image built behind one,
# say) would only send local traffic — Browser Use's own CDP websocket to its
# browser on loopback — to an address that does not exist here, so none is
# honoured.
for _name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "WS_PROXY", "WSS_PROXY"):
    os.environ.pop(_name, None)
    os.environ.pop(_name.lower(), None)

os.environ.update({
    "ANONYMIZED_TELEMETRY": "false",
    "BROWSER_USE_CLOUD_SYNC": "false",
    "BROWSER_USE_LOGGING_LEVEL": "warning",
    "HOME": "/scratch/home",
    "XDG_CONFIG_HOME": "/scratch/home/.config",
    "XDG_CACHE_HOME": "/scratch/home/.cache",
    "BROWSER_USE_CONFIG_DIR": "/scratch/home/.config/browseruse",
    "TMPDIR": "/scratch/tmp",
    "IN_DOCKER": "true",
})

from protocol import read_task, result_document  # noqa: E402

SCRATCH = "/scratch"
MODEL_SOCKET = os.environ.get("JARVIS_MODEL_SOCKET", "/run/jarvis/model.sock")
EGRESS_SOCKET = os.environ.get("JARVIS_EGRESS_SOCKET", "/run/jarvis/egress.sock")
PROXY_PORT = 18080
CHROMIUM = os.environ.get("JARVIS_CHROMIUM", "/usr/bin/chromium")


def _write_result(document: str) -> None:
    with open(os.path.join(SCRATCH, "result.json"), "w", encoding="utf-8") as handle:
        handle.write(document)


async def main() -> int:
    for directory in ("home", "home/.config", "home/.cache", "tmp", "profile"):
        os.makedirs(os.path.join(SCRATCH, directory), exist_ok=True)
    try:
        with open(os.path.join(SCRATCH, "task.json"), encoding="utf-8") as handle:
            task = read_task(handle.read())
    except (OSError, ValueError):
        _write_result(result_document(status="failed", final=None, steps=0, error="task_unreadable"))
        return 2
    token = os.environ.get("JARVIS_RUN_TOKEN", "")

    from browser_use import Agent
    from browser_use.browser.profile import BrowserProfile, ProxySettings

    from forwarder import serve
    from jarvis_llm import JarvisChatModel

    forwarder = await serve(EGRESS_SOCKET, PROXY_PORT)
    llm = JarvisChatModel(socket_path=MODEL_SOCKET, run_token=token)
    profile = BrowserProfile(
        headless=True,
        executable_path=CHROMIUM,
        user_data_dir=os.path.join(SCRATCH, "profile"),
        proxy=ProxySettings(server=f"http://127.0.0.1:{PROXY_PORT}"),
        allowed_domains=list(task["hosts"]),       # advisory: the proxy decides
        chromium_sandbox=False,                    # gVisor is the sandbox
        enable_default_extensions=False,           # no downloads: nothing reaches out but the proxy
        args=["--disable-dev-shm-usage", "--no-first-run", "--disable-background-networking",
              "--disable-component-update", "--disable-sync", "--metrics-recording-only"],
    )
    # Browser Use's own judge is a second opinion from the same model, never
    # authority — JARVIS reads only the result, as data — so it is off.
    agent = Agent(task=task["task"], llm=llm, browser_profile=profile, use_vision=False, use_judge=False)
    steps, status, final, error = 0, "failed", None, None
    try:
        history = await agent.run(max_steps=task["max_steps"])
        steps = history.number_of_steps()
        final = history.final_result()
        status = "completed" if history.is_done() and final is not None else "failed"
        if status == "failed":
            error = "not_done"
    except Exception as exc:  # noqa: BLE001 — the run's failure is data for JARVIS, never a crash it trusts
        error = type(exc).__name__
    finally:
        forwarder.close()
        await llm.aclose()
    _write_result(result_document(status=status, final=final, steps=steps, error=error))
    return 0 if status == "completed" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
