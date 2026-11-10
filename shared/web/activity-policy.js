// The character's activity policy for web clients (OD-CHAR-4) — the third
// implementation, beside the Python reference (`ActivityPolicy` in
// shared/schemas/presentation.py) and Android (`ActivityPolicy.kt`).
//
// All three are held to the same specification:
//   shared/android/presentation_activity_policy_vectors.json  (the policy)
//   shared/android/presentation_activity_samples.json         (the wire report)
//   shared/android/presentation_intensity_vectors.json        (intensity)
// and the JavaScript tests also replay random input sequences against the
// Python reference (tests/presentation/test_activity_policy_js_parity.py).
//
// Used by the desktop companion (desktop/) and, later, the web character page.
// Plain ES module, no dependencies, no I/O: every input carries `nowMs`, a
// monotonic millisecond clock. Nothing here is stored, logged or sent.
//
// An expression presents JARVIS's operational activity. It is not a feeling,
// not a mood that persists, not a measurement of the user, never the status,
// and it grants nothing.

/** The seven states, in the owner's order — also the tie-break order. */
export const EXPRESSIONS = Object.freeze([
  "happy", "satisfied", "default", "sleepy", "surprised", "angry", "sad",
]);
export const DEFAULT = "default";
export const SLEEPY = "sleepy";
export const SURPRISED = "surprised";
/** States with a maximum display time: an event, not a condition. */
export const TRANSIENT_STATES = Object.freeze(new Set([SURPRISED]));

export const SCORE_MIN = 1;
export const SCORE_MAX = 10;
export const INTENSITY_MIN = 1;
export const INTENSITY_MAX = 10;
export const INTENSITY_NEUTRAL = 5;

const COMPONENTS = Object.freeze([
  "engagement", "productivity", "steadiness", "failure", "pressure", "surprise", "inactivity",
]);

/** A supplied state, validated: anything unknown is `default`, never an error. */
export function parseExpression(value) {
  return typeof value === "string" && EXPRESSIONS.includes(value) ? value : DEFAULT;
}

// ── strict JSON ──────────────────────────────────────────────────────────
//
// `JSON.parse` cannot tell `8` from `8.0`, and the contract can: a score is a
// JSON integer, and `8.0` is a float the Python and Kotlin parsers reject.
// This small recursive-descent parser keeps each number's lexeme so the
// validators below can apply the same rule. It also refuses duplicate keys
// (stricter than the reference, never more permissive) and nesting deeper
// than any document here needs.

class JsonNumber {
  constructor(text) {
    this.text = text;
    this.value = Number(text);
  }

  get isInteger() {
    return /^-?(0|[1-9][0-9]*)$/.test(this.text);
  }
}

const MAX_DEPTH = 8;
const MAX_TEXT = 65_536;

class StrictJsonError extends Error {}

function parseStrictJson(text) {
  if (typeof text !== "string" || text.length > MAX_TEXT) throw new StrictJsonError("not text");
  let i = 0;
  const ws = () => {
    while (i < text.length && " \t\n\r".includes(text[i])) i += 1;
  };
  const fail = () => {
    throw new StrictJsonError(`unexpected input at ${i}`);
  };
  const value = (depth) => {
    if (depth > MAX_DEPTH) fail();
    ws();
    const c = text[i];
    if (c === "{") return object(depth);
    if (c === "[") return array(depth);
    if (c === '"') return string();
    if (c === "-" || (c >= "0" && c <= "9")) return number();
    for (const [word, v] of [["true", true], ["false", false], ["null", null]]) {
      if (text.startsWith(word, i)) {
        i += word.length;
        return v;
      }
    }
    return fail();
  };
  const object = (depth) => {
    i += 1;
    const out = Object.create(null);
    ws();
    if (text[i] === "}") {
      i += 1;
      return out;
    }
    for (;;) {
      ws();
      if (text[i] !== '"') fail();
      const key = string();
      if (Object.prototype.hasOwnProperty.call(out, key)) fail();
      ws();
      if (text[i] !== ":") fail();
      i += 1;
      out[key] = value(depth + 1);
      ws();
      if (text[i] === ",") {
        i += 1;
        continue;
      }
      if (text[i] === "}") {
        i += 1;
        return out;
      }
      fail();
    }
  };
  const array = (depth) => {
    i += 1;
    const out = [];
    ws();
    if (text[i] === "]") {
      i += 1;
      return out;
    }
    for (;;) {
      out.push(value(depth + 1));
      ws();
      if (text[i] === ",") {
        i += 1;
        continue;
      }
      if (text[i] === "]") {
        i += 1;
        return out;
      }
      fail();
    }
  };
  const string = () => {
    i += 1;
    let out = "";
    for (;;) {
      if (i >= text.length) fail();
      const c = text[i];
      if (c === '"') {
        i += 1;
        return out;
      }
      if (c < " ") fail();
      if (c === "\\") {
        const e = text[i + 1];
        const simple = { '"': '"', "\\": "\\", "/": "/", b: "\b", f: "\f", n: "\n", r: "\r", t: "\t" };
        if (e in simple) {
          out += simple[e];
          i += 2;
        } else if (e === "u" && /^[0-9a-fA-F]{4}$/.test(text.slice(i + 2, i + 6))) {
          out += String.fromCharCode(parseInt(text.slice(i + 2, i + 6), 16));
          i += 6;
        } else {
          fail();
        }
      } else {
        out += c;
        i += 1;
      }
    }
  };
  const number = () => {
    const m = /^-?(0|[1-9][0-9]*)(\.[0-9]+)?([eE][+-]?[0-9]+)?/.exec(text.slice(i));
    if (!m) fail();
    i += m[0].length;
    return new JsonNumber(m[0]);
  };
  const result = value(0);
  ws();
  if (i !== text.length) fail();
  return result;
}

// ── validation helpers ───────────────────────────────────────────────────

class InvalidDocument extends Error {}

function isPlainObject(v) {
  return v !== null && typeof v === "object" && !Array.isArray(v) && !(v instanceof JsonNumber);
}

/** An integer in [low, high]: a JSON integer lexeme, or a JS safe integer. */
function int(v, low, high, where) {
  let n;
  if (v instanceof JsonNumber) {
    if (!v.isInteger) throw new InvalidDocument(`${where}: not an integer`);
    n = v.value;
  } else if (typeof v === "number" && Number.isSafeInteger(v)) {
    n = v;
  } else {
    throw new InvalidDocument(`${where}: not an integer`);
  }
  if (!(n >= low && n <= high)) throw new InvalidDocument(`${where}: out of range`);
  return n;
}

function num(v, low, high, where) {
  const n = v instanceof JsonNumber ? v.value : v;
  if (typeof n !== "number" || !Number.isFinite(n) || n < low || n > high) {
    throw new InvalidDocument(`${where}: out of range`);
  }
  return n;
}

function exactKeys(obj, keys, where) {
  if (!isPlainObject(obj)) throw new InvalidDocument(`${where}: not an object`);
  const have = Object.keys(obj);
  if (have.length !== keys.length || !keys.every((k) => Object.prototype.hasOwnProperty.call(obj, k))) {
    throw new InvalidDocument(`${where}: fields`);
  }
}

function onlyKeys(obj, keys, where) {
  if (!isPlainObject(obj)) throw new InvalidDocument(`${where}: not an object`);
  for (const k of Object.keys(obj)) {
    if (!keys.includes(k)) throw new InvalidDocument(`${where}: unknown field ${k}`);
  }
}

// RFC 3339 date-time with a zone ('T' separator; seconds optional; up to nine
// fraction digits). The calendar is checked, so "2026-02-30" is refused.
const TIMESTAMP = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})(?::(\d{2})(?:\.\d{1,9})?)?(Z|z|[+-](\d{2}):(\d{2}))$/;

function isAwareTimestamp(value) {
  if (typeof value !== "string") return false;
  const m = TIMESTAMP.exec(value);
  if (!m) return false;
  const [, y, mo, d, h, mi, s, , oh, om] = m;
  const year = Number(y);
  const month = Number(mo);
  const day = Number(d);
  const leap = (year % 4 === 0 && year % 100 !== 0) || year % 400 === 0;
  const days = [31, leap ? 29 : 28, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
  if (year < 1 || month < 1 || month > 12 || day < 1 || day > days[month - 1]) return false;
  if (Number(h) > 23 || Number(mi) > 59 || (s !== undefined && Number(s) > 59)) return false;
  if (oh !== undefined && (Number(oh) > 23 || Number(om) > 59)) return false;
  return true;
}

// ── the activity report ─────────────────────────────────────────────────

const REPORT_FIELDS = ["schema_version", "computed_at", "window_s", "scores", "components"];

/**
 * One activity report (`GET /api/v1/presentation/activity`), validated whole:
 * a missing, extra, mistyped or out-of-range field — or anything that is not a
 * report — rejects it (`null`). Returns `{scores}` only: the components are
 * validated with the rest and then dropped, as on Android; `computed_at` is
 * informational (staleness is judged from receipt, on the client's own clock).
 */
export function parseActivityReport(text) {
  try {
    const root = parseStrictJson(text);
    exactKeys(root, REPORT_FIELDS, "report");
    if (int(root.schema_version, 1, 1, "schema_version") !== 1) return null;
    if (!isAwareTimestamp(root.computed_at)) return null;
    int(root.window_s, 60, 86_400, "window_s");
    exactKeys(root.components, COMPONENTS, "components");
    for (const c of COMPONENTS) int(root.components[c], 0, 100, `components.${c}`);
    exactKeys(root.scores, EXPRESSIONS, "scores");
    const scores = {};
    for (const e of EXPRESSIONS) scores[e] = int(root.scores[e], SCORE_MIN, SCORE_MAX, `scores.${e}`);
    return Object.freeze({ scores: Object.freeze(scores) });
  } catch (err) {
    if (err instanceof StrictJsonError || err instanceof InvalidDocument) return null;
    throw err;
  }
}

/** Scores for a test or a preview, validated like a parsed report. */
export function reportOf(scores) {
  const out = {};
  for (const e of EXPRESSIONS) {
    out[e] = int(scores[e], SCORE_MIN, SCORE_MAX, `scores.${e}`);
  }
  if (Object.keys(scores).length !== EXPRESSIONS.length) throw new InvalidDocument("scores: fields");
  return Object.freeze({ scores: Object.freeze(out) });
}

// ── settings (CharacterPresentationSettings) ────────────────────────────

const HOLD_STATES = ["happy", "satisfied", "sleepy", "surprised", "angry", "sad"];

export const DEFAULT_SETTINGS = deepFreeze({
  schema_version: 1,
  visible: true,
  profile_id: "default",
  transition: "crossfade",
  reduced_motion: "system",
  min_confidence: 0.6,
  thresholds: { happy: 7, satisfied: 7, default: 1, sleepy: 8, surprised: 7, angry: 8, sad: 8 },
  timing: {
    transition_ms: 220,
    min_display_ms: 1500,
    hold_ms: { happy: 5000, satisfied: 5000, sleepy: 5000, surprised: 2500, angry: 4000, sad: 5000 },
    idle_timeout_ms: 300000,
    interrupt_score: 9,
    max_result_age_ms: 5000,
  },
  intensity: { floor: 4, dead_band: 2, baseline: { default: 5, sleepy: 2 } },
  activity: {
    poll_interval_ms: 15000,
    max_age_ms: 60000,
    exit_margin: 2,
    cooldown_ms: { happy: 30000, satisfied: 15000, sleepy: 30000, surprised: 60000, angry: 60000, sad: 30000 },
  },
});

function deepFreeze(o) {
  for (const v of Object.values(o)) if (v && typeof v === "object") deepFreeze(v);
  return Object.freeze(o);
}

function section(doc, key, defaults, build, where) {
  if (!Object.prototype.hasOwnProperty.call(doc, key)) return defaults;
  return build(doc[key], `${where}.${key}`);
}

function intMap(obj, keys, low, high, defaults, where) {
  onlyKeys(obj, keys, where);
  const out = {};
  for (const k of keys) {
    out[k] = Object.prototype.hasOwnProperty.call(obj, k) ? int(obj[k], low, high, `${where}.${k}`) : defaults[k];
  }
  return out;
}

/**
 * A presentation-settings document (an object, or its JSON text), validated
 * by the same rules as `CharacterPresentationSettings`: an unknown field, a
 * wrong type or an out-of-range value rejects the whole document (throws).
 * Absent sections and fields take the defaults.
 */
export function parseSettings(input) {
  const doc = typeof input === "string" ? parseStrictJson(input) : input;
  const d = DEFAULT_SETTINGS;
  const where = "settings";
  onlyKeys(doc, ["schema_version", "visible", "profile_id", "transition", "reduced_motion", "min_confidence",
    "thresholds", "timing", "intensity", "activity"], where);
  const pick = (k, check) => (Object.prototype.hasOwnProperty.call(doc, k) ? check(doc[k], `${where}.${k}`) : d[k]);
  const bool = (v, w) => {
    if (typeof v !== "boolean") throw new InvalidDocument(`${w}: not a boolean`);
    return v;
  };
  const oneOf = (values) => (v, w) => {
    if (!values.includes(v)) throw new InvalidDocument(`${w}: not one of ${values}`);
    return v;
  };
  const out = {
    schema_version: pick("schema_version", (v, w) => int(v, 1, 1, w)),
    visible: pick("visible", bool),
    profile_id: pick("profile_id", (v, w) => {
      if (typeof v !== "string" || !/^[a-z0-9][a-z0-9_-]{0,63}$/.test(v)) throw new InvalidDocument(w);
      return v;
    }),
    transition: pick("transition", oneOf(["crossfade", "none"])),
    reduced_motion: pick("reduced_motion", oneOf(["system", "always"])),
    min_confidence: pick("min_confidence", (v, w) => num(v, 0, 1, w)),
    thresholds: section(doc, "thresholds", d.thresholds,
      (v, w) => intMap(v, EXPRESSIONS, SCORE_MIN, SCORE_MAX, d.thresholds, w), where),
    timing: section(doc, "timing", d.timing, (v, w) => {
      onlyKeys(v, ["transition_ms", "min_display_ms", "hold_ms", "idle_timeout_ms", "interrupt_score",
        "max_result_age_ms"], w);
      const t = (k, low, high) => (Object.prototype.hasOwnProperty.call(v, k) ? int(v[k], low, high, `${w}.${k}`)
        : d.timing[k]);
      return {
        transition_ms: t("transition_ms", 0, 2000),
        min_display_ms: t("min_display_ms", 0, 30000),
        hold_ms: section(v, "hold_ms", d.timing.hold_ms,
          (h, hw) => intMap(h, HOLD_STATES, 500, 60000, d.timing.hold_ms, hw), w),
        idle_timeout_ms: t("idle_timeout_ms", 10000, 86400000),
        interrupt_score: t("interrupt_score", SCORE_MIN, SCORE_MAX),
        max_result_age_ms: t("max_result_age_ms", 100, 60000),
      };
    }, where),
    intensity: section(doc, "intensity", d.intensity, (v, w) => {
      onlyKeys(v, ["floor", "dead_band", "baseline"], w);
      return {
        floor: Object.prototype.hasOwnProperty.call(v, "floor") ? int(v.floor, INTENSITY_MIN, INTENSITY_MAX, `${w}.floor`)
          : d.intensity.floor,
        dead_band: Object.prototype.hasOwnProperty.call(v, "dead_band") ? int(v.dead_band, 0, 9, `${w}.dead_band`)
          : d.intensity.dead_band,
        baseline: section(v, "baseline", d.intensity.baseline,
          (b, bw) => intMap(b, ["default", "sleepy"], INTENSITY_MIN, INTENSITY_MAX, d.intensity.baseline, bw), w),
      };
    }, where),
    activity: section(doc, "activity", d.activity, (v, w) => {
      onlyKeys(v, ["poll_interval_ms", "max_age_ms", "exit_margin", "cooldown_ms"], w);
      const a = (k, low, high) => (Object.prototype.hasOwnProperty.call(v, k) ? int(v[k], low, high, `${w}.${k}`)
        : d.activity[k]);
      const result = {
        poll_interval_ms: a("poll_interval_ms", 5000, 300000),
        max_age_ms: a("max_age_ms", 10000, 600000),
        exit_margin: a("exit_margin", 0, 5),
        cooldown_ms: section(v, "cooldown_ms", d.activity.cooldown_ms,
          (c, cw) => intMap(c, HOLD_STATES, 0, 600000, d.activity.cooldown_ms, cw), w),
      };
      if (result.max_age_ms <= result.poll_interval_ms) {
        throw new InvalidDocument(`${w}: max_age_ms must exceed poll_interval_ms`);
      }
      return result;
    }, where),
  };
  return deepFreeze(out);
}

export { InvalidDocument };

// ── intensity ────────────────────────────────────────────────────────────

/**
 * How strongly a selected expression is presented (1–10):
 *   floor + round_half_up((score − threshold) × (10 − floor) / (10 − threshold))
 * Integer arithmetic only. Throws for a value out of range or a score below
 * its threshold (the selector never selects one).
 */
export function presentationIntensity(score, threshold, floor) {
  for (const [name, v, low, high] of [
    ["score", score, SCORE_MIN, SCORE_MAX],
    ["threshold", threshold, SCORE_MIN, SCORE_MAX],
    ["floor", floor, INTENSITY_MIN, INTENSITY_MAX],
  ]) {
    if (typeof v !== "number" || !Number.isSafeInteger(v) || v < low || v > high) {
      throw new RangeError(`${name} out of range`);
    }
  }
  if (score < threshold) throw new RangeError("the score does not meet its threshold");
  if (threshold === SCORE_MAX) return INTENSITY_MAX;
  const span = SCORE_MAX - threshold;
  const numerator = (score - threshold) * (INTENSITY_MAX - floor);
  return floor + Math.floor((2 * numerator + span) / (2 * span));
}

/** The fixed intensity of a baseline state (`default` or `sleepy`). */
export function baselineIntensity(expression, settings) {
  if (expression !== DEFAULT && expression !== SLEEPY) throw new RangeError("not a baseline state");
  return settings.intensity.baseline[expression];
}

/** The shown state selected again keeps its intensity inside the dead band. */
export function retainedIntensity(current, candidate, deadBand) {
  return Math.abs(candidate - current) >= deadBand ? candidate : current;
}

// ── the policy ───────────────────────────────────────────────────────────

/**
 * The deterministic presentation policy every client runs (OD-CHAR-4); see
 * docs/CHARACTER_PRESENTATION.md §3 and the Python reference for the rules.
 * In memory only.
 */
export class ActivityPolicy {
  constructor(settings, nowMs) {
    this.settings = settings;
    this.shown = DEFAULT;
    this.sinceMs = nowMs;
    this.intensity = baselineIntensity(DEFAULT, settings);
    this._scores = null;
    this._receivedMs = null;
    // Starting the app is local activity, and the character starts at the baseline.
    this._localMs = nowMs;
    this._lastExit = new Map();
  }

  /** A report: its text (validated here), an already-parsed report, or null. */
  onReport(report, nowMs) {
    const parsed = typeof report === "string" ? parseActivityReport(report) : report;
    if (parsed) {
      this._scores = parsed.scores;
      this._receivedMs = nowMs;
    }
    this._evaluate(nowMs);
  }

  /** Local activity: a click on the character, opening JARVIS, waking from sleep. */
  onLocalActivity(nowMs) {
    this._localMs = nowMs;
    this._evaluate(nowMs);
  }

  tick(nowMs) {
    this._evaluate(nowMs);
  }

  /** The earliest time a tick could change something without new input. */
  nextChangeAt(nowMs) {
    const s = this.settings;
    const times = [this._localMs + s.timing.idle_timeout_ms];
    if (this._receivedMs !== null) times.push(this._receivedMs + s.activity.max_age_ms + 1);
    if (this.shown !== DEFAULT) times.push(this.sinceMs + s.timing.min_display_ms);
    if (TRANSIENT_STATES.has(this.shown)) times.push(this.sinceMs + this._hold(this.shown));
    for (const [e, left] of this._lastExit) times.push(left + this._cooldown(e));
    const future = times.filter((t) => t > nowMs);
    return future.length ? Math.min(...future) : null;
  }

  _hold(e) {
    return this.settings.timing.hold_ms[e];
  }

  _cooldown(e) {
    return e === DEFAULT ? 0 : this.settings.activity.cooldown_ms[e];
  }

  _evaluate(now) {
    const s = this.settings;
    const fresh = this._receivedMs !== null && now - this._receivedMs <= s.activity.max_age_ms;
    if (!fresh || this._scores === null) {
      const idle = now - this._localMs >= s.timing.idle_timeout_ms;
      const target = idle ? SLEEPY : DEFAULT;
      this._commit(target, baselineIntensity(target, s), now);
      return;
    }

    const scores = { ...this._scores };
    if (now - this._localMs < s.timing.idle_timeout_ms) scores[SLEEPY] = SCORE_MIN;
    const threshold = (e) => s.thresholds[e];
    const cooling = (e) => {
      const left = this._lastExit.get(e);
      return left !== undefined && now - left < this._cooldown(e);
    };

    const shown = this.shown;
    const holds = shown !== DEFAULT
      && scores[shown] >= threshold(shown) - s.activity.exit_margin
      && !(TRANSIENT_STATES.has(shown) && now - this.sinceMs >= this._hold(shown));
    let best = holds ? shown : null;
    for (const e of EXPRESSIONS) { // declaration order = tie-break order
      if (e === DEFAULT || e === shown) continue;
      if (scores[e] < threshold(e) || cooling(e)) continue;
      if (best === null || scores[e] > scores[best]) best = e;
    }
    let target = best || DEFAULT;

    if (target !== shown && holds) {
      const settled = now - this.sinceMs >= s.timing.min_display_ms;
      if (!settled && scores[target] < s.timing.interrupt_score) target = shown;
    }
    this._commit(target, this._intensityFor(target, scores), now);
  }

  _intensityFor(e, scores) {
    if (e === DEFAULT) return baselineIntensity(e, this.settings);
    const threshold = this.settings.thresholds[e];
    return presentationIntensity(Math.max(scores[e], threshold), threshold, this.settings.intensity.floor);
  }

  _commit(target, intensity, now) {
    if (target === this.shown) {
      this.intensity = retainedIntensity(this.intensity, intensity, this.settings.intensity.dead_band);
      return;
    }
    if (this.shown !== DEFAULT) this._lastExit.set(this.shown, now);
    this.shown = target;
    this.intensity = intensity;
    this.sinceMs = now;
  }
}
