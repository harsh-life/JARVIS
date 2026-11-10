// The webview boundary, checked from the page side: the pages make no
// network request, keep nothing in browser storage, and call only their own
// window's commands. (The Rust side checks the capability files.)
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const DESKTOP = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const SRC = join(DESKTOP, "src");
const read = (p) => readFileSync(p, "utf8");
const pageScripts = readdirSync(SRC).filter((f) => f.endsWith(".js")).map((f) => [f, read(join(SRC, f))]);
pageScripts.push(["activity-policy.js", read(join(DESKTOP, "..", "shared", "web", "activity-policy.js"))]);

const capability = (name) => JSON.parse(read(join(DESKTOP, "src-tauri", "capabilities", `${name}.json`))).permissions
  .filter((p) => p.startsWith("allow-")).map((p) => p.slice(6).replaceAll("-", "_"));

test("no page script can reach the network, storage, the clipboard or eval", () => {
  const forbidden = [/\bfetch\s*\(/, /XMLHttpRequest/, /WebSocket/, /EventSource/, /sendBeacon/, /localStorage/,
    /sessionStorage/, /indexedDB/, /document\.cookie/, /navigator\.clipboard/, /\beval\s*\(/, /new Function/,
    /\.innerHTML\s*=/, /insertAdjacentHTML/, /window\.open\s*\(/, /importScripts/];
  for (const [file, text] of pageScripts) {
    for (const pattern of forbidden) assert.ok(!pattern.test(text), `${file}: ${pattern}`);
  }
});

test("each page invokes only its own window's commands", () => {
  const calls = (text) => [...text.matchAll(/invoke\(\s*"([a-z_]+)"/g)].map((m) => m[1]);
  const companion = new Set(capability("companion"));
  const settings = new Set(capability("settings"));
  for (const c of calls(read(join(SRC, "companion.js")))) assert.ok(companion.has(c), `companion.js calls ${c}`);
  for (const c of calls(read(join(SRC, "settings.js")))) assert.ok(settings.has(c), `settings.js calls ${c}`);
  assert.ok(calls(read(join(SRC, "companion.js"))).length > 0);
  // No command is shared between the two windows.
  for (const c of companion) assert.ok(!settings.has(c), c);
});

test("no page mentions a credential", () => {
  for (const [file, text] of pageScripts) {
    for (const word of ["access_token", "bootstrap", "device_credential", "code_verifier", "Authorization", "Bearer"]) {
      assert.ok(!text.includes(word), `${file} mentions ${word}`);
    }
  }
});

test("the pages load only their own scripts and styles", () => {
  for (const page of ["index.html", "settings.html"]) {
    const html = read(join(SRC, page));
    assert.ok(!/<script(?![^>]*\bsrc=")/i.test(html), `${page}: inline script`);
    assert.ok(!/(src|href)="(https?:)?\/\//i.test(html), `${page}: external resource`);
    assert.ok(!/\son[a-z]+=/i.test(html), `${page}: inline event handler`);
  }
});

test("the companion never persists a report or an expression", () => {
  const companion = read(join(SRC, "companion.js"));
  assert.ok(!/console\.(log|info|debug)/.test(companion), "nothing is logged");
  // The only command that carries the expression is the smoke-test status,
  // and it is sent only in smoke mode.
  assert.match(companion, /if \(smoke\) invoke\("companion_status"/);
});
