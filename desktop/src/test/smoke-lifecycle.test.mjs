// The CI smoke harness (scripts/smoke-lifecycle.mjs) judges initialization
// and termination separately, and on a hang kills only the process it started.
import { test } from "node:test";
import assert from "node:assert/strict";
import { spawn, spawnSync } from "node:child_process";
import { mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import { runSmoke } from "../../scripts/smoke-lifecycle.mjs";

const HARNESS = join(dirname(fileURLToPath(import.meta.url)), "..", "..", "scripts", "smoke-lifecycle.mjs");
const dir = mkdtempSync(join(tmpdir(), "smoke-lifecycle-"));
test.after(() => rmSync(dir, { recursive: true, force: true }));

// A stand-in for the app: `node fake.mjs <mode> --smoke-test <result>`.
const FAKE = join(dir, "fake.mjs");
writeFileSync(FAKE, `
const [mode, , out] = process.argv.slice(2);
const { writeFileSync } = await import("node:fs");
const write = (ok) => writeFileSync(out, JSON.stringify({ ok, window: ok, renderer: ok, shown: ok ? "happy" : null }));
if (mode === "pass") { write(true); process.exit(0); }
if (mode === "hang") { write(true); setInterval(() => {}, 1000); }
if (mode === "fail") { write(false); process.exit(1); }
if (mode === "ok-but-status-1") { write(true); process.exit(1); }
if (mode === "status-0-no-result") { process.exit(0); }
`);

const run = (mode, boundMs = 10_000) =>
  runSmoke({ binary: process.execPath, args: [FAKE, mode], result: join(dir, `${mode}.json`), boundMs, log: () => {} });

const alive = (pid) => {
  try {
    process.kill(pid, 0);
    return true;
  } catch {
    return false;
  }
};

test("a run that reports success and exits 0 passes", async () => {
  const s = await run("pass");
  assert.equal(s.initialized, true);
  assert.equal(s.terminated, true);
  assert.equal(s.exit_code, 0);
  assert.equal(s.passed, true);
});

test("a success report from a process that never exits is a failure, and only that PID is killed", async () => {
  // A bystander running the same program, not started by the harness.
  const bystander = spawn(process.execPath, [FAKE, "hang", "--smoke-test", join(dir, "bystander.json")], { stdio: "ignore" });
  try {
    const s = await run("hang", 1500);
    assert.equal(s.initialized, true, "the result file did say ok");
    assert.equal(s.terminated, false);
    assert.equal(s.killed_by_harness, true);
    assert.equal(s.passed, false);
    assert.match(s.failures.join(";"), /still running after 1.5s; killed PID \d+/);
    assert.equal(alive(s.pid), false, "the harness's own process is gone");
    assert.equal(alive(bystander.pid), true, "an unrelated process is untouched");
  } finally {
    bystander.kill();
  }
});

test("a reported failure fails", async () => {
  const s = await run("fail");
  assert.equal(s.initialized, false);
  assert.equal(s.terminated, true);
  assert.equal(s.passed, false);
});

test("ok: true with a non-zero status fails", async () => {
  const s = await run("ok-but-status-1");
  assert.equal(s.initialized, true);
  assert.equal(s.terminated, true);
  assert.equal(s.exit_code, 1);
  assert.equal(s.passed, false);
});

test("status 0 without a result file fails", async () => {
  const s = await run("status-0-no-result");
  assert.equal(s.initialized, false);
  assert.equal(s.exit_code, 0);
  assert.equal(s.passed, false);
});

test("the command line exits non-zero for a hang and zero for a pass", () => {
  const cli = (mode) =>
    spawnSync(process.execPath, [HARNESS, "--result", join(dir, `cli-${mode}.json`), "--bound-s", "1.5", "--", process.execPath, FAKE, mode], { encoding: "utf8" });
  const hang = cli("hang");
  assert.equal(hang.status, 1);
  assert.match(hang.stdout, /smoke test: FAILED — still running after 1.5s/);
  const pass = cli("pass");
  assert.equal(pass.status, 0, pass.stdout + pass.stderr);
  assert.match(pass.stdout, /smoke test: PASSED/);
});
