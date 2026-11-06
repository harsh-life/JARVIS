"""Pilot plan C11 static checks, applied to the Workbench's browser client:
served at `/ui` with a strict CSP, no inline script or style, nothing loaded
from another origin, no token persisted anywhere, no markup built from
strings, and no authorization logic in the page."""

from __future__ import annotations

import re
from pathlib import Path

from server.gateway.web_ui import SECURITY_HEADERS, WEB_ROOT

FILES = sorted(p for p in WEB_ROOT.rglob("*") if p.is_file())
JS = [p for p in FILES if p.suffix == ".js"]


def test_the_client_exists_and_is_plain_files():
    assert (WEB_ROOT / "index.html").is_file()
    assert JS, "no modules"
    assert not (WEB_ROOT / "package.json").exists() and not (WEB_ROOT / "node_modules").exists()


def test_no_inline_script_or_style_and_no_other_origin():
    html = (WEB_ROOT / "index.html").read_text()
    for tag in re.findall(r"<script\b[^>]*>(.*?)</script>", html, flags=re.S):
        assert tag.strip() == "", "inline script"
    assert "<style" not in html and " style=" not in html
    assert not re.search(r"\son[a-z]+=", html), "inline event handler"
    for path in FILES:
        text = path.read_text()
        assert not re.search(r"https?://", text), f"{path.name} references another origin"


def test_no_token_persistence_no_string_markup_no_eval():
    for path in JS:
        text = path.read_text()
        for forbidden in ("localStorage", "sessionStorage", "document.cookie", "indexedDB", "innerHTML",
                          "outerHTML", "insertAdjacentHTML", "eval(", "new Function", "setAttribute(\"style\""):
            assert forbidden not in text, f"{path.name} uses {forbidden}"


def test_no_authorization_logic_in_the_client():
    # The page renders what the server says; it never decides a tier, a
    # capability or whether something is allowed.
    for path in JS:
        text = path.read_text()
        for forbidden in ("risk_tier", "low_read", "consequential", "grant", "capabilit" + "y_id"):
            assert forbidden not in text, f"{path.name} mentions {forbidden}"


async def test_ui_is_served_with_the_security_headers(make_harness):
    h = await make_harness()
    for path in ("/ui/", "/ui/js/app.js", "/ui/styles.css"):
        resp = await h.client.get(path)
        assert resp.status_code == 200, path
        for name, value in SECURITY_HEADERS.items():
            assert resp.headers[name] == value
    csp = SECURITY_HEADERS["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp and "unsafe" not in csp
    assert (await h.client.get("/ui/../pyproject.toml")).status_code == 404
