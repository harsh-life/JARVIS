// The sprite renderer and the pinned pixel-art atlas (OD-DESK-2).
import { test } from "node:test";
import assert from "node:assert/strict";
import { createHash } from "node:crypto";
import { readFileSync } from "node:fs";
import { inflateSync } from "node:zlib";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  EXPRESSIONS, LONG_IDLE_MS, SpriteAnimator, bandFor, integerScale, resolveAnimation, validateAtlas,
} from "../renderer.js";

const DESKTOP = join(dirname(fileURLToPath(import.meta.url)), "..", "..");
const ROOT = join(DESKTOP, "..");
const ATLAS = JSON.parse(readFileSync(join(DESKTOP, "src/sprites/jarvis-atlas.json"), "utf8"));

/** Decode an 8-bit RGBA, non-interlaced PNG (what the generator writes). */
function decodePng(path) {
  const buf = readFileSync(path);
  let pos = 8;
  let width = 0;
  let height = 0;
  const idat = [];
  while (pos < buf.length) {
    const len = buf.readUInt32BE(pos);
    const type = buf.toString("ascii", pos + 4, pos + 8);
    const data = buf.subarray(pos + 8, pos + 8 + len);
    if (type === "IHDR") {
      width = data.readUInt32BE(0);
      height = data.readUInt32BE(4);
      assert.equal(data[8], 8, "8-bit");
      assert.equal(data[9], 6, "RGBA");
      assert.equal(data[12], 0, "not interlaced");
    }
    if (type === "IDAT") idat.push(data);
    pos += 12 + len;
  }
  const raw = inflateSync(Buffer.concat(idat));
  const stride = width * 4;
  const out = Buffer.alloc(stride * height);
  for (let y = 0; y < height; y += 1) {
    const f = raw[y * (stride + 1)];
    const line = raw.subarray(y * (stride + 1) + 1, (y + 1) * (stride + 1));
    for (let x = 0; x < stride; x += 1) {
      const a = x >= 4 ? out[y * stride + x - 4] : 0;
      const b = y > 0 ? out[(y - 1) * stride + x] : 0;
      const c = x >= 4 && y > 0 ? out[(y - 1) * stride + x - 4] : 0;
      const p = a + b - c;
      const pr = Math.abs(p - a) <= Math.abs(p - b) && Math.abs(p - a) <= Math.abs(p - c) ? a
        : Math.abs(p - b) <= Math.abs(p - c) ? b : c;
      const pred = [0, a, b, (a + b) >> 1, pr][f];
      out[y * stride + x] = (line[x] + pred) & 0xff;
    }
  }
  return { width, height, pixels: out };
}

const PNG = decodePng(join(DESKTOP, "src/sprites/jarvis-atlas.png"));

test("the atlas and its fallback match their SHA-256 pins", () => {
  for (const line of readFileSync(join(DESKTOP, "art/SHA256SUMS"), "utf8").trim().split("\n")) {
    const [digest, rel] = line.split(/\s+/);
    assert.equal(createHash("sha256").update(readFileSync(join(ROOT, rel))).digest("hex"), digest, rel);
  }
});

test("every frame index is inside the image and on the grid", () => {
  assert.deepEqual(ATLAS.image_size, [PNG.width, PNG.height]);
  const check = validateAtlas(ATLAS, [PNG.width, PNG.height]);
  assert.deepEqual(check.errors, []);
});

test("all seven expressions load, with every animation the renderer asks for", () => {
  assert.deepEqual(ATLAS.expressions, EXPRESSIONS);
  for (const e of EXPRESSIONS) {
    for (const name of [`static_${e}`, `idle_${e}`, `idle_${e}_low`, `idle_${e}_high`, `to_${e}`, `long_idle_${e}`]) {
      assert.ok(ATLAS.animations[name], name);
    }
    const idle = ATLAS.animations[`idle_${e}`];
    assert.ok(idle.frames.length >= 4 && idle.frames.length <= 8, `${e}: 4–8 idle frames`);
    assert.ok(ATLAS.animations[`to_${e}`].frames.length >= 2 && ATLAS.animations[`to_${e}`].frames.length <= 4);
    assert.equal(idle.loop, true);
    assert.equal(ATLAS.animations[`to_${e}`].loop, false);
  }
});

test("pixel art: hard alpha and a controlled palette", () => {
  const colours = new Set();
  for (let i = 0; i < PNG.pixels.length; i += 4) {
    const alpha = PNG.pixels[i + 3];
    assert.ok(alpha === 0 || alpha === 255, "no partial transparency");
    if (alpha) colours.add(PNG.pixels.readUInt32BE(i));
  }
  assert.ok(colours.size <= ATLAS.palette.length, `${colours.size} colours, palette ${ATLAS.palette.length}`);
});

test("intensity only picks a band", () => {
  assert.deepEqual([1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map(bandFor),
    ["low", "low", "low", "normal", "normal", "normal", "normal", "high", "high", "high"]);
});

test("integer scaling at every display scale", () => {
  for (const dpr of [1, 1.25, 1.5, 1.75, 2, 2.5, 3]) {
    for (const scale of [2, 3, 4]) {
      const k = integerScale(scale, dpr);
      assert.ok(Number.isInteger(k) && k >= scale, `${scale}× at ${dpr}`);
    }
  }
  assert.equal(integerScale(2, 1), 2);
  assert.equal(integerScale(2, 2), 4);
});

test("missing animations fall back; a minimal atlas still renders", () => {
  assert.equal(resolveAnimation(ATLAS, "idle", "furious", "normal"), "idle_default");
  const minimal = { frame_size: [80, 96], frames: { a: { x: 0, y: 0 } },
    animations: { static_default: { frames: ["a"], durations_ms: [1000], loop: false } } };
  assert.equal(resolveAnimation(minimal, "idle", "angry", "high"), "static_default");
  assert.equal(resolveAnimation(minimal, "to", "angry", "high"), null);
  const anim = new SpriteAnimator(minimal, 0);
  anim.setTarget("sad", 9, 10);
  assert.equal(anim.frameAt(5000), "a");
  assert.equal(validateAtlas({ frame_size: [80, 96], frames: {}, animations: {} }, [80, 96]).ok, false);
  assert.equal(validateAtlas({ ...ATLAS, frames: { ...ATLAS.frames, bad: { x: 10_000, y: 0 } } }, [PNG.width, PNG.height]).ok, false);
});

const framesOf = (names) => new Set(names.flatMap((n) => ATLAS.animations[n].frames));

test("a new expression plays its transition, then idles — never flicking back", () => {
  const a = new SpriteAnimator(ATLAS, 0);
  a.setTarget("angry", 8, 1000);
  const allowed = framesOf(["to_angry", "idle_angry_high", "long_idle_angry"]);
  const seen = [];
  for (let t = 1000; t < 6000; t += 20) {
    const f = a.frameAt(t);
    assert.ok(allowed.has(f), `frame ${f} at ${t} is not angry's`);
    seen.push(f);
  }
  assert.equal(seen[0], ATLAS.animations.to_angry.frames[0]);
  assert.ok(seen.some((f) => framesOf(["idle_angry_high"]).has(f)));
});

test("a band change keeps the same face without restarting", () => {
  const a = new SpriteAnimator(ATLAS, 0);
  a.setTarget("happy", 5, 0);
  a.frameAt(2000);
  a.setTarget("happy", 9, 2000);   // same expression, higher intensity
  const f = a.frameAt(2001);
  assert.ok(framesOf(["idle_happy_high"]).has(f));
  assert.ok(!ATLAS.animations.to_happy.frames.includes(f) || framesOf(["idle_happy_high"]).has(f));
});

test("reduced motion: the right expression, still", () => {
  const a = new SpriteAnimator(ATLAS, 0, { reducedMotion: true });
  a.setTarget("sad", 6, 100);
  const first = a.frameAt(100);
  assert.equal(first, ATLAS.animations.idle_sad.frames[0]);
  for (let t = 100; t < 30_000; t += 777) assert.equal(a.frameAt(t), first);
  assert.equal(a.nextFrameAt(200), null);
  a.poke(300);
  assert.equal(a.frameAt(400), first);
  assert.equal(a.frameAt(100 + LONG_IDLE_MS + 1), first, "no long-idle animation either");
});

test("hidden: nothing is scheduled", () => {
  const a = new SpriteAnimator(ATLAS, 0);
  a.setVisible(false, 10);
  assert.equal(a.nextFrameAt(20), null);
  a.setVisible(true, 30);
  assert.notEqual(a.nextFrameAt(40), null);
});

test("long idle plays once after ten minutes, then returns to the idle loop", () => {
  const a = new SpriteAnimator(ATLAS, 0);
  a.setTarget("satisfied", 6, 0);
  const t0 = LONG_IDLE_MS + 5000;
  assert.ok(framesOf(["long_idle_satisfied"]).has(a.frameAt(t0)));
  const total = ATLAS.animations.long_idle_satisfied.durations_ms.reduce((x, y) => x + y, 0);
  assert.ok(framesOf(["idle_satisfied"]).has(a.frameAt(t0 + total + 50)));
  assert.ok(framesOf(["idle_satisfied"]).has(a.frameAt(t0 + total + 60_000)), "not again within ten minutes");
});

test("frames advance only when one is due", () => {
  const a = new SpriteAnimator(ATLAS, 0);
  a.setTarget("default", 5, 0);
  const next = a.nextFrameAt(5);
  assert.ok(next > 5 && next - 5 <= 1000);
});
