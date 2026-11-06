"""The browser client's static files, served at `/ui` (pilot plan C11).

Plain files from the repository's `web/` directory — no templating, no
server-side data, nothing that authenticates. Every response carries a strict
Content-Security-Policy (`default-src 'self'`, no inline script, no framing),
so a page can talk only to this origin's API. The page holds the access token
in memory only; it never writes it to storage.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from starlette.responses import Response
from starlette.staticfiles import StaticFiles
from starlette.types import Scope

UI_PATH = "/ui"
WEB_ROOT = Path(__file__).resolve().parents[2] / "web"

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; object-src 'none'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
    "Cache-Control": "no-store",
}


class _SecureStatic(StaticFiles):
    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers.update(SECURITY_HEADERS)
        return response


def mount_web_ui(app: FastAPI, root: Path = WEB_ROOT) -> bool:
    """Mount `web/` at `/ui` if it is present (a deployment without the
    directory simply has no browser client)."""

    if not (root / "index.html").is_file():
        return False
    app.mount(UI_PATH, _SecureStatic(directory=str(root), html=True), name="web-ui")
    return True


__all__ = ["SECURITY_HEADERS", "UI_PATH", "WEB_ROOT", "mount_web_ui"]
