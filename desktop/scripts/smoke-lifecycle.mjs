// Runs one `--smoke-test` of the companion and judges it on three separate
// conditions, so a success message is never mistaken for a finished run:
//
//   initialized  the result file exists and says `ok: true`
//   terminated   the process ended by itself within the bound
//   exit_code    it ended with status 0
//
// The run passes only when all three hold. If the process is still alive at
// the bound, the harness signals that one process — the PID it started, never
// a process name or group — and the run fails.
//
// Usage (CI, .github/workflows/desktop.yml):
//   node scripts/smoke-lifecycle.mjs --result <file.json> [--bound-s 90]
//        [--screenshot-after-s 6 --screenshot "<shell command>"] -- <binary> [args...]
// `--smoke-test <file.json>` is appended to the binary's arguments.
import { spawn, spawnSync } from "node:child_process";
import { existsSync, readFileSync } from "node:fs";
import { pathToFileURL } from "node:url";

const KILL_GRACE_MS = 5000;

function readResult(path) {
  if (!existsSync(path)) return { result: null, error: "no result file" };
  try {
    return { result: JSON.parse(readFileSync(path, "utf8")), error: null };
  } catch (e) {
    return { result: null, error: `unreadable result file: ${e.message}` };
  }
}

export async function runSmoke({ binary, args = [], result, boundMs = 90_000, screenshot = null, screenshotAfterMs = 6000, log = console.log }) {
  const started = Date.now();
  const child = spawn(binary, [...args, "--smoke-test", result], { stdio: "inherit" });
  const exited = new Promise((resolve) => {
    child.once("exit", (code, signal) => resolve({ code, signal }));
    child.once("error", (error) => resolve({ code: null, signal: null, error }));
  });
  let shot = null;
  if (screenshot) {
    shot = setTimeout(() => {
      const r = spawnSync(screenshot, { shell: true, stdio: "inherit" });
      if (r.status !== 0) log(`screenshot command exited ${r.status} (not a smoke failure)`);
    }, screenshotAfterMs);
  }
  const timedOut = Symbol("timeout");
  let timer;
  const first = await Promise.race([exited, new Promise((r) => (timer = setTimeout(() => r(timedOut), boundMs)))]);
  clearTimeout(timer);
  if (shot) clearTimeout(shot);

  const summary = { pid: child.pid, bound_s: boundMs / 1000 };
  if (first === timedOut) {
    summary.terminated = false;
    summary.alive_at_s = (Date.now() - started) / 1000;
    child.kill("SIGTERM"); // this PID only
    let forced;
    const gone = await Promise.race([exited, new Promise((r) => (forced = setTimeout(() => r(null), KILL_GRACE_MS)))]);
    clearTimeout(forced);
    if (gone === null) {
      child.kill("SIGKILL");
      await exited;
    }
    summary.killed_by_harness = true;
    summary.exit_code = null;
  } else {
    summary.terminated = first.error === undefined;
    summary.elapsed_s = (Date.now() - started) / 1000;
    summary.exit_code = first.code;
    if (first.signal) summary.signal = first.signal;
    if (first.error) summary.spawn_error = first.error.message;
  }

  const { result: record, error } = readResult(result);
  summary.initialized = record !== null && record.ok === true;
  if (error) summary.result_error = error;
  summary.result = record;
  summary.passed = summary.initialized && summary.terminated && summary.exit_code === 0;
  const why = [];
  if (!summary.initialized) why.push(error ?? "result says ok: false");
  if (!summary.terminated) why.push(summary.killed_by_harness ? `still running after ${summary.bound_s}s; killed PID ${summary.pid}` : "did not start");
  if (summary.terminated && summary.exit_code !== 0) why.push(`exited with status ${summary.exit_code}`);
  summary.failures = why;
  return summary;
}

function parse(argv) {
  const sep = argv.indexOf("--");
  if (sep < 0 || sep === argv.length - 1) throw new Error("usage: smoke-lifecycle.mjs --result <file> [options] -- <binary> [args...]");
  const opts = {};
  const flags = argv.slice(0, sep);
  for (let i = 0; i < flags.length; i += 2) opts[flags[i]] = flags[i + 1];
  if (!opts["--result"]) throw new Error("--result is required");
  return {
    binary: argv[sep + 1],
    args: argv.slice(sep + 2),
    result: opts["--result"],
    boundMs: Number(opts["--bound-s"] ?? 90) * 1000,
    screenshot: opts["--screenshot"] ?? null,
    screenshotAfterMs: Number(opts["--screenshot-after-s"] ?? 6) * 1000,
  };
}

if (import.meta.url === pathToFileURL(process.argv[1]).href) {
  const summary = await runSmoke(parse(process.argv.slice(2)));
  console.log(JSON.stringify(summary, null, 2));
  console.log(summary.passed ? "smoke test: PASSED (initialized, terminated, status 0)" : `smoke test: FAILED — ${summary.failures.join("; ")}`);
  process.exit(summary.passed ? 0 : 1);
}
