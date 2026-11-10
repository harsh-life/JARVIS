// The JavaScript activity policy against the shared specification (OD-CHAR-4).
// Mirrors tests/presentation/test_activity_policy.py case for case; run with
//   node --test shared/web/
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join } from "node:path";

import {
  ActivityPolicy,
  DEFAULT_SETTINGS,
  EXPRESSIONS,
  TRANSIENT_STATES,
  baselineIntensity,
  parseActivityReport,
  parseExpression,
  parseSettings,
  presentationIntensity,
  reportOf,
  retainedIntensity,
} from "./activity-policy.js";

const ROOT = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const read = (p) => JSON.parse(readFileSync(join(ROOT, p), "utf8"));
const VECTORS = read("shared/android/presentation_activity_policy_vectors.json");
const SAMPLES = read("shared/android/presentation_activity_samples.json");
const INTENSITY = read("shared/android/presentation_intensity_vectors.json");
const BASE_SCORES = { happy: 1, satisfied: 1, default: 5, sleepy: 1, surprised: 1, angry: 1, sad: 1 };
const COMPONENTS = { engagement: 0, productivity: 0, steadiness: 0, failure: 0, pressure: 0, surprise: 0, inactivity: 0 };

function merge(base, extra) {
  const out = structuredClone(base);
  for (const [k, v] of Object.entries(extra)) {
    out[k] = v && typeof v === "object" && !Array.isArray(v) && out[k] && typeof out[k] === "object"
      ? merge(out[k], v) : v;
  }
  return out;
}

const settingsWith = (overrides) => parseSettings(merge(structuredClone(DEFAULT_SETTINGS), overrides || {}));

export function report(scores) {
  const s = scores === "invalid" ? { ...BASE_SCORES, happy: 11 } : { ...BASE_SCORES, ...scores };
  return JSON.stringify({ schema_version: 1, computed_at: "2026-11-10T12:00:00Z", window_s: 1800, scores: s,
    components: COMPONENTS });
}

function apply(policy, step) {
  if ("report" in step) policy.onReport(report(step.report), step.at);
  else if (step.activity) policy.onLocalActivity(step.at);
  else policy.tick(step.at);
}

// ── the shared vectors ──────────────────────────────────────────────────

for (const c of VECTORS.cases) {
  test(`vector: ${c.name}`, () => {
    const policy = new ActivityPolicy(settingsWith(c.settings), 0);
    c.steps.forEach((step, i) => {
      apply(policy, step);
      assert.deepEqual([policy.shown, policy.intensity], step.expect, `step ${i} at ${step.at}`);
    });
  });
}

test("the vectors reach every state", () => {
  const reached = new Set(VECTORS.cases.flatMap((c) => c.steps.map((s) => s.expect[0])));
  assert.deepEqual([...reached].sort(), [...EXPRESSIONS].sort());
});

for (const sample of SAMPLES.valid) {
  test(`valid report: ${sample.name}`, () => assert.notEqual(parseActivityReport(sample.text), null));
}
for (const sample of SAMPLES.invalid) {
  test(`invalid report: ${sample.name}`, () => assert.equal(parseActivityReport(sample.text), null));
  test(`an invalid report never moves the character: ${sample.name}`, () => {
    const policy = new ActivityPolicy(DEFAULT_SETTINGS, 0);
    policy.onReport(report({ happy: 9 }), 0);
    policy.onReport(sample.text, 1000);
    assert.deepEqual([policy.shown, policy.intensity], ["happy", 8]);
  });
}

test("reports the JSON parser alone could not refuse", () => {
  const ok = JSON.parse(report({ happy: 8 }));
  const variant = (mutate) => { const r = structuredClone(ok); mutate(r); return JSON.stringify(r); };
  assert.notEqual(parseActivityReport(report({ happy: 8 })), null);
  assert.equal(parseActivityReport(report({ happy: 8 }).replace('"happy": 8', '"happy": 8.0')
    .replace('"happy":8', '"happy":8.0')), null);
  assert.equal(parseActivityReport(report({}).replace('"window_s":1800', '"window_s":1.8e3')), null);
  assert.equal(parseActivityReport(variant((r) => { r.computed_at = "2026-02-30T12:00:00Z"; })), null);
  assert.equal(parseActivityReport(variant((r) => { r.computed_at = 1790000000; })), null);
  assert.equal(parseActivityReport('{"schema_version":1,"schema_version":1}'), null);   // duplicate key
  assert.equal(parseActivityReport(null), null);
  assert.equal(parseActivityReport(`${report({})} trailing`), null);
  assert.equal(parseActivityReport("[".repeat(50)), null);
});

// ── intensity vectors ───────────────────────────────────────────────────

for (const v of INTENSITY.mapping) {
  test(`intensity: ${v.name}`, () => {
    if (v.expected === null) assert.throws(() => presentationIntensity(v.score, v.threshold, v.floor));
    else assert.equal(presentationIntensity(v.score, v.threshold, v.floor), v.expected);
  });
}
test("baseline intensities and the dead band", () => {
  for (const b of INTENSITY.baseline) assert.equal(baselineIntensity(b.state, DEFAULT_SETTINGS), b.expected);
  for (const d of INTENSITY.dead_band) assert.equal(retainedIntensity(d.current, d.candidate, d.dead_band), d.expected);
});

// ── properties beyond the vectors ───────────────────────────────────────

function rng(seed) {   // mulberry32: deterministic, dependency-free
  let a = seed >>> 0;
  return () => {
    a = (a + 0x6D2B79F5) >>> 0;
    let t = a;
    t = Math.imul(t ^ (t >>> 15), t | 1);
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61);
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
}

function randomRun(seed) {
  const r = rng(seed);
  const policy = new ActivityPolicy(DEFAULT_SETTINGS, 0);
  let now = 0;
  const changes = [];
  for (let i = 0; i < 400; i += 1) {
    now += [500, 1000, 5000, 15000, 60000][Math.floor(r() * 5)];
    const roll = r();
    if (roll < 0.7) {
      const scores = Object.fromEntries(EXPRESSIONS.map((e) => [e, 1 + Math.floor(r() * 10)]));
      policy.onReport(report(scores), now);
    } else if (roll < 0.8) policy.onReport("not a report", now);
    else if (roll < 0.9) policy.onLocalActivity(now);
    else policy.tick(now);
    changes.push([now, policy.shown, policy.intensity]);
  }
  return changes;
}

test("any input sequence stays bounded and respects cooldowns", () => {
  for (let seed = 0; seed < 25; seed += 1) {
    const left = new Map();
    let previous = "default";
    for (const [now, shown, intensity] of randomRun(seed)) {
      assert.ok(intensity >= 1 && intensity <= 10);
      if (shown !== previous) {
        if (previous !== "default") left.set(previous, now);
        if (left.has(shown) && shown !== "sleepy" && shown !== "default") {
          assert.ok(now - left.get(shown) >= DEFAULT_SETTINGS.activity.cooldown_ms[shown]);
        }
      }
      previous = shown;
    }
  }
});

test("the same inputs always give the same presentation", () => {
  assert.deepEqual(randomRun(7), randomRun(7));
});

test("surprised is the only transient state", () => {
  assert.deepEqual([...TRANSIENT_STATES], ["surprised"]);
});

test("nextChangeAt names the staleness and the surprise hold", () => {
  const policy = new ActivityPolicy(DEFAULT_SETTINGS, 0);
  policy.onReport(report({ surprised: 10 }), 1000);
  assert.equal(policy.nextChangeAt(1000), 2500);
  policy.tick(2500);
  assert.equal(policy.nextChangeAt(2500), 3500);
  policy.tick(3500);
  assert.equal(policy.shown, "default");
  assert.equal(policy.nextChangeAt(3500), 61001);
});

test("an unknown expression is default", () => {
  assert.equal(parseExpression("furious"), "default");
  assert.equal(parseExpression(3), "default");
  assert.equal(parseExpression("sad"), "sad");
});

test("reportOf validates like a parsed report", () => {
  assert.throws(() => reportOf({ ...BASE_SCORES, happy: 0 }));
  assert.equal(reportOf(BASE_SCORES).scores.default, 5);
});

// ── settings ────────────────────────────────────────────────────────────

test("the shipped default settings are the built-in defaults", () => {
  const shipped = parseSettings(readFileSync(join(ROOT, "assets/character/presentation.json"), "utf8"));
  assert.deepEqual(shipped, parseSettings({}));
  assert.deepEqual(shipped, DEFAULT_SETTINGS);
});

test("the activity block is optional", () => {
  const doc = structuredClone(DEFAULT_SETTINGS);
  delete doc.activity;
  assert.deepEqual(parseSettings(doc), DEFAULT_SETTINGS);
});

const INVALID_SETTINGS = [
  { thresholds: { happy: 0 } }, { thresholds: { sad: 11 } }, { thresholds: { happy: 7.5 } },
  { thresholds: { furious: 5 } }, { min_confidence: 1.1 }, { timing: { transition_ms: -1 } },
  { timing: { idle_timeout_ms: 1000 } }, { timing: { interrupt_score: 0 } },
  { timing: { hold_ms: { surprised: 100 } } }, { timing: { hold_ms: { default: 1000 } } },
  { reduced_motion: "sometimes" }, { transition: "spin" }, { schema_version: 2 }, { expression: "happy" },
  { activity: { poll_interval_ms: 1000 } }, { activity: { max_age_ms: 700000 } },
  { activity: { poll_interval_ms: 60000, max_age_ms: 60000 } }, { activity: { exit_margin: 6 } },
  { activity: { exit_margin: -1 } }, { activity: { cooldown_ms: { angry: -1 } } },
  { activity: { cooldown_ms: { default: 1000 } } }, { activity: { mood_decay: 5 } },
  { activity: { exit_margin: "2" } }, { visible: 1 },
];
for (const overrides of INVALID_SETTINGS) {
  test(`invalid settings are rejected: ${JSON.stringify(overrides)}`, () => {
    assert.throws(() => parseSettings(merge(structuredClone(DEFAULT_SETTINGS), overrides)));
  });
}
