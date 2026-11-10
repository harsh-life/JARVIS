// The pixel-art sprite renderer (OD-DESK-2).
//
// `SpriteAnimator` is pure: given an atlas index, the expression and intensity
// the shared ActivityPolicy chose, and a monotonic clock, it says which frame
// to show and when the next one is due. `CanvasRenderer` draws a frame with
// integer scaling and no smoothing. Nothing here selects an expression —
// intensity only picks an animation band (1–3 low, 4–7 normal, 8–10 high).

export const EXPRESSIONS = ["happy", "satisfied", "default", "sleepy", "surprised", "angry", "sad"];
export const LONG_IDLE_MS = 10 * 60 * 1000;
const POKE_MS = 360;

/** Intensity band: the only thing intensity decides here. */
export function bandFor(intensity) {
  if (intensity <= 3) return "low";
  if (intensity >= 8) return "high";
  return "normal";
}

/**
 * Device pixels per sprite pixel: an integer, so every sprite pixel is a
 * whole block of physical pixels at any display scale.
 */
export function integerScale(scale, devicePixelRatio) {
  return Math.max(1, Math.round(scale * (devicePixelRatio || 1)));
}

/** Check an atlas index against its image size; list every problem. */
export function validateAtlas(atlas, imageSize) {
  const errors = [];
  if (!atlas || typeof atlas !== "object") return { ok: false, errors: ["not an atlas"] };
  const [fw, fh] = atlas.frame_size || [];
  if (!Number.isInteger(fw) || !Number.isInteger(fh) || fw <= 0 || fh <= 0) errors.push("frame_size");
  const [iw, ih] = imageSize || atlas.image_size || [];
  for (const [name, f] of Object.entries(atlas.frames || {})) {
    if (!Number.isInteger(f.x) || !Number.isInteger(f.y) || f.x < 0 || f.y < 0 || f.x + fw > iw || f.y + fh > ih
        || f.x % fw !== 0 || f.y % fh !== 0) {
      errors.push(`frame ${name} is outside the image or off the grid`);
    }
  }
  for (const [name, a] of Object.entries(atlas.animations || {})) {
    if (!Array.isArray(a.frames) || a.frames.length === 0) errors.push(`animation ${name} has no frames`);
    else {
      if (!Array.isArray(a.durations_ms) || a.durations_ms.length !== a.frames.length) errors.push(`animation ${name} durations`);
      for (const d of a.durations_ms || []) if (!Number.isInteger(d) || d < 16 || d > 10000) errors.push(`animation ${name} duration ${d}`);
      for (const f of a.frames) if (!(atlas.frames || {})[f]) errors.push(`animation ${name} names a missing frame ${f}`);
    }
  }
  if (!atlas.animations || !atlas.animations.static_default) errors.push("static_default is missing");
  return { ok: errors.length === 0, errors };
}

/** The animation to play, with fallbacks: never a missing name. */
export function resolveAnimation(atlas, kind, expression, band) {
  const a = atlas.animations;
  const e = EXPRESSIONS.includes(expression) ? expression : "default";
  const candidates = kind === "idle"
    ? [band === "normal" ? `idle_${e}` : `idle_${e}_${band}`, `idle_${e}`, "idle_default", `static_${e}`, "static_default"]
    : [`${kind}_${e}`, null];
  for (const name of candidates) if (name && a[name]) return name;
  return null;
}

export class SpriteAnimator {
  constructor(atlas, nowMs, { reducedMotion = false } = {}) {
    this.atlas = atlas;
    this.reducedMotion = reducedMotion;
    this.visible = true;
    this.expression = "default";
    this.band = "normal";
    this._changedAt = nowMs;
    this._play(resolveAnimation(atlas, "idle", "default", "normal"), nowMs, true);
  }

  /** The policy's choice. A new expression plays its transition, then idles. */
  setTarget(expression, intensity, nowMs) {
    const e = EXPRESSIONS.includes(expression) ? expression : "default";
    const band = bandFor(intensity);
    if (e === this.expression) {
      if (band !== this.band) {
        // Same face, new band: switch animation on the next frame boundary,
        // keeping the phase — no restart, no flicker.
        this.band = band;
        if (this._kind === "idle") this._swapKeepingPhase(resolveAnimation(this.atlas, "idle", e, band), nowMs);
      }
      return;
    }
    this.expression = e;
    this.band = band;
    this._changedAt = nowMs;
    const to = this.reducedMotion ? null : resolveAnimation(this.atlas, "to", e, band);
    if (to) {
      this._play(to, nowMs, false, "to");
    } else {
      this._play(resolveAnimation(this.atlas, "idle", e, band), nowMs, true, "idle");
    }
  }

  setReducedMotion(on, nowMs) {
    this.reducedMotion = on;
    if (on) this._play(resolveAnimation(this.atlas, "idle", this.expression, this.band), nowMs, true, "idle");
  }

  setVisible(visible, nowMs) {
    this.visible = visible;
    if (visible) this._started = nowMs;   // resume from the first frame
  }

  /** A click: a small hop on the current expression (never a new one). */
  poke(nowMs) {
    if (this.reducedMotion) return;
    const to = resolveAnimation(this.atlas, "to", this.expression, this.band);
    if (to && this._kind === "idle") this._play(to, nowMs, false, "to");
    this._pokedAt = nowMs;
  }

  /** The frame to draw now. */
  frameAt(nowMs) {
    this._advance(nowMs);
    const anim = this.atlas.animations[this._name];
    if (this.reducedMotion || !this.visible) return anim.frames[0];
    return anim.frames[this._indexAt(nowMs)];
  }

  /** When the frame next changes; null when nothing will (hidden, reduced motion). */
  nextFrameAt(nowMs) {
    if (!this.visible) return null;
    this._advance(nowMs);
    if (this.reducedMotion) return null;
    const anim = this.atlas.animations[this._name];
    const total = anim.durations_ms.reduce((a, b) => a + b, 0);
    let t = nowMs - this._started;
    if (anim.loop) t %= total;
    let acc = 0;
    for (const d of anim.durations_ms) {
      acc += d;
      if (t < acc) return nowMs + (acc - t);
    }
    return nowMs + 1;
  }

  _play(name, nowMs, loop, kind = "idle") {
    this._name = name;
    this._started = nowMs;
    this._kind = kind;
    this._loop = loop;
  }

  _swapKeepingPhase(name, nowMs) {
    const old = this.atlas.animations[this._name];
    const fresh = this.atlas.animations[name];
    const oldTotal = old.durations_ms.reduce((a, b) => a + b, 0);
    const newTotal = fresh.durations_ms.reduce((a, b) => a + b, 0);
    const phase = ((nowMs - this._started) % oldTotal) / oldTotal;
    this._name = name;
    this._started = nowMs - Math.floor(phase * newTotal);
  }

  _indexAt(nowMs) {
    const anim = this.atlas.animations[this._name];
    const total = anim.durations_ms.reduce((a, b) => a + b, 0);
    let t = nowMs - this._started;
    if (anim.loop) t %= total;
    let acc = 0;
    for (let i = 0; i < anim.durations_ms.length; i += 1) {
      acc += anim.durations_ms[i];
      if (t < acc) return i;
    }
    return anim.frames.length - 1;
  }

  _advance(nowMs) {
    if (this.reducedMotion) return;
    const anim = this.atlas.animations[this._name];
    const total = anim.durations_ms.reduce((a, b) => a + b, 0);
    if (!anim.loop && nowMs - this._started >= total) {
      // A transition or long idle finished: back to the idle loop.
      this._play(resolveAnimation(this.atlas, "idle", this.expression, this.band), this._started + total, true, "idle");
    }
    if (this._kind === "idle" && nowMs - Math.max(this._changedAt, this._longIdleAt || 0) >= LONG_IDLE_MS) {
      const long = resolveAnimation(this.atlas, "long_idle", this.expression, this.band);
      this._longIdleAt = nowMs;
      if (long) this._play(long, nowMs, false, "long_idle");
    }
  }
}

/** Draws atlas frames onto a canvas with integer scaling and no smoothing. */
export class CanvasRenderer {
  constructor(canvas, image, atlas) {
    this.canvas = canvas;
    this.image = image;
    this.atlas = atlas;
    this.ctx = canvas.getContext("2d");
    this.k = 1;
    this.backdrop = false;
  }

  resize(scale, devicePixelRatio) {
    const [fw, fh] = this.atlas.frame_size;
    this.k = integerScale(scale, devicePixelRatio);
    this.canvas.width = fw * this.k;
    this.canvas.height = fh * this.k;
    this.canvas.style.width = `${(fw * this.k) / (devicePixelRatio || 1)}px`;
    this.canvas.style.height = `${(fh * this.k) / (devicePixelRatio || 1)}px`;
  }

  draw(frameName) {
    const ctx = this.ctx;
    const [fw, fh] = this.atlas.frame_size;
    ctx.imageSmoothingEnabled = false;
    ctx.clearRect(0, 0, this.canvas.width, this.canvas.height);
    if (this.backdrop) {
      ctx.fillStyle = "rgba(28, 30, 38, 0.82)";
      const r = 6 * this.k;
      const w = this.canvas.width;
      const h = this.canvas.height;
      ctx.beginPath();
      ctx.moveTo(r, 0); ctx.lineTo(w - r, 0); ctx.quadraticCurveTo(w, 0, w, r); ctx.lineTo(w, h - r);
      ctx.quadraticCurveTo(w, h, w - r, h); ctx.lineTo(r, h); ctx.quadraticCurveTo(0, h, 0, h - r);
      ctx.lineTo(0, r); ctx.quadraticCurveTo(0, 0, r, 0); ctx.fill();
    }
    const f = this.atlas.frames[frameName];
    ctx.drawImage(this.image, f.x, f.y, fw, fh, 0, 0, fw * this.k, fh * this.k);
  }
}
