#!/usr/bin/env python3
"""Generate the desktop companion's pixel-art sprite atlas (OD-DESK-2).

Reproducible: the only inputs are the owner-approved originals in
`assets/character/default/` (read, never modified) and the hand-authored rig in
`desktop/art/rig.json` (feature anchors in sprite pixels). Output:

    desktop/src/sprites/jarvis-atlas.png   — every unique frame, packed on a grid
    desktop/src/sprites/jarvis-atlas.json  — frame index + animations
    desktop/art/SHA256SUMS                 — pins of both, checked in CI

Pipeline, per expression:
  1. drop the drawing's decorations that are not the character (the "Zzz" of
     `sleepy`; the companion animates its own), keep the largest silhouette;
  2. area-downsample (premultiplied) to fit 64×88, bottom-centred;
  3. binarise alpha, quantise every expression to ONE shared palette
     (median cut, no dithering) plus a few fixed effect colours;
  4. add a 1 px dark outline outside the sticker rim, for any wallpaper.
Animations are then built from those bases by integer pixel operations only
(bob, breathe, shake, squash, blink, tears, particles), so the palette and the
character's proportions never drift.

Run:  python desktop/art/generate_sprites.py           (writes the files)
      python desktop/art/generate_sprites.py --check   (regenerates in memory and
                                                        compares pixels; exit 1 on drift)
Requires Pillow==12.3.0 and numpy (desktop/art/requirements.txt).
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import sys
from collections import deque
from pathlib import Path

import numpy as np
from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "assets" / "character" / "default"
RIG = Path(__file__).with_name("rig.json")
OUT_DIR = ROOT / "desktop" / "src" / "sprites"
ATLAS_PNG = OUT_DIR / "jarvis-atlas.png"
ATLAS_JSON = OUT_DIR / "jarvis-atlas.json"
SUMS = Path(__file__).with_name("SHA256SUMS")

EXPRESSIONS = ["happy", "satisfied", "default", "sleepy", "surprised", "angry", "sad"]
SPRITE_W, SPRITE_H = 64, 88          # the character
FRAME_W, FRAME_H = 80, 96            # the frame: headroom for particles and bobbing
OFF_X, OFF_Y = (FRAME_W - SPRITE_W) // 2, FRAME_H - SPRITE_H - 1
PALETTE_SIZE = 36
OUTLINE = (24, 22, 30, 255)
WAIST = 60                            # rows above this settle when the body breathes
FX = {                                # fixed effect colours (not from the art)
    "zzz": (74, 123, 216, 255),
    "zzz_light": (160, 196, 245, 255),
    "spark": (255, 214, 92, 255),
    "spark_core": (255, 248, 220, 255),
    "tear": (120, 186, 240, 255),
    "steam": (210, 214, 222, 255),
    "steam_dark": (150, 156, 168, 255),
    "alert": (238, 84, 72, 255),
    "white": (255, 255, 255, 255),
}


# ── source preparation ──────────────────────────────────────────────────

def _largest_component(mask: np.ndarray) -> np.ndarray:
    h, w = mask.shape
    label = np.zeros((h, w), np.int32)
    best_size, best = 0, 0
    n = 0
    for y0, x0 in zip(*np.nonzero(mask)):
        if label[y0, x0]:
            continue
        n += 1
        label[y0, x0] = n
        queue, size = deque([(y0, x0)]), 0
        while queue:
            y, x = queue.popleft()
            size += 1
            for yy, xx in ((y + 1, x), (y - 1, x), (y, x + 1), (y, x - 1)):
                if 0 <= yy < h and 0 <= xx < w and mask[yy, xx] and not label[yy, xx]:
                    label[yy, xx] = n
                    queue.append((yy, xx))
        if size > best_size:
            best_size, best = size, n
    return label == best


def _strip_decoration(a: np.ndarray, rule: dict) -> None:
    """Clear blue/white decoration pixels left of the hood edge in a band."""

    r, g, b = a[..., 0].astype(int), a[..., 1].astype(int), a[..., 2].astype(int)
    grey = (abs(r - g) < 12) & (abs(g - b) < 14) & (r > 70) & (r < 200) & (a[..., 3] > 200)
    blue = (b > r + 30) & (b > g + 10)
    white = (r > 205) & (g > 205) & (b > 205)
    y0, y1, margin = rule["y_from"], rule["y_to"], rule["margin"]
    for y in range(y0, min(y1, a.shape[0])):
        xs = np.nonzero(grey[y])[0]
        if len(xs) == 0:
            continue
        # blue lettering and bubbles: up to the fur itself
        cut = max(xs.min(), 0)
        segment = a[y, :cut]          # a view: writing it clears the source pixels
        segment[blue[y, :cut], 3] = 0
        # their white sticker edge: up to the hood's own outline and rim
        cut = max(xs.min() - margin, 0)
        segment = a[y, :cut]
        segment[white[y, :cut], 3] = 0


def _base(expression: str, rig: dict) -> np.ndarray:
    """The 64×88 RGBA sprite, before palette quantisation."""

    meta = rig["expressions"][expression]
    im = Image.open(SRC / meta["source"]).convert("RGBA")
    a = np.array(im)
    if "strip" in meta:
        _strip_decoration(a, meta["strip"])
    # keep the largest silhouette (judged at quarter scale for speed)
    small = Image.fromarray(a).resize((im.width // 4, im.height // 4), Image.BOX)
    keep = _largest_component(np.array(small)[..., 3] > 128)
    keep_full = np.array(Image.fromarray(keep.astype(np.uint8) * 255).resize(im.size, Image.NEAREST)) > 0
    a[~keep_full, 3] = 0
    im = Image.fromarray(a)
    box = Image.fromarray(((a[..., 3] > 128) * 255).astype(np.uint8)).getbbox()
    im = im.crop(box)
    scale = min((SPRITE_W - 2) / im.width, (SPRITE_H - 2) / im.height)
    w, h = max(1, round(im.width * scale)), max(1, round(im.height * scale))
    small = im.convert("RGBa").resize((w, h), Image.BOX).convert("RGBA")
    s = np.array(small)
    s[..., 3] = np.where(s[..., 3] >= 110, 255, 0)
    out = np.zeros((SPRITE_H, SPRITE_W, 4), np.uint8)
    x, y = (SPRITE_W - w) // 2, SPRITE_H - 1 - h
    out[y:y + h, x:x + w] = s
    return out


def _quantise(bases: dict[str, np.ndarray]) -> tuple[dict[str, np.ndarray], list[tuple[int, int, int]]]:
    stacked = np.concatenate([b.reshape(-1, 4) for b in bases.values()])
    opaque = stacked[stacked[:, 3] == 255][:, :3].astype(np.int32)
    # The expressions live in a few small, saturated details — blush, mouth,
    # tears — that a pixel-count median cut would merge into the skin. They
    # are over-sampled so the shared palette keeps them.
    saturation = opaque.max(1) - opaque.min(1)
    vivid = opaque[saturation > 40]
    weighted = np.concatenate([opaque] + [vivid] * 12)
    sample = Image.fromarray(weighted.reshape(1, -1, 3).astype(np.uint8), "RGB")
    pal_img = sample.quantize(colors=PALETTE_SIZE, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE)
    flat = pal_img.getpalette()[: PALETTE_SIZE * 3]
    palette = [tuple(flat[i:i + 3]) for i in range(0, len(flat), 3)]
    pal = np.array(palette, np.int32)
    out = {}
    for name, b in bases.items():
        rgb = b[..., :3].astype(np.int32)
        d = ((rgb[:, :, None, :] - pal[None, None, :, :]) ** 2).sum(-1)
        q = pal[d.argmin(-1)].astype(np.uint8)
        res = np.zeros_like(b)
        res[..., :3] = q
        res[..., 3] = b[..., 3]
        res[b[..., 3] == 0] = 0
        out[name] = res
    return out, palette


def _outline(sprite: np.ndarray) -> np.ndarray:
    a = sprite[..., 3] > 0
    grown = a.copy()
    grown[1:, :] |= a[:-1, :]
    grown[:-1, :] |= a[1:, :]
    grown[:, 1:] |= a[:, :-1]
    grown[:, :-1] |= a[:, 1:]
    ring = grown & ~a
    out = sprite.copy()
    out[ring] = OUTLINE
    return out


def _patch(sprite: np.ndarray, patches: list[dict]) -> np.ndarray:
    out = sprite.copy()
    for p in patches:
        x0, y0, x1, y1 = p["rect"]
        colour = out[p["sample"][1], p["sample"][0]].copy()
        region = out[y0:y1 + 1, x0:x1 + 1]
        light = (region[..., 3] > 0) & (region[..., :3].astype(int).min(-1) > 170)
        region[light] = colour
    return out


def _head(sprite: np.ndarray) -> tuple[int, int]:
    """The top of the hood: the first opaque row, at the sprite's centre column."""

    rows = np.nonzero(sprite[..., 3].any(1))[0]
    return SPRITE_W // 2, int(rows[0]) if len(rows) else 0


# ── frame operations (integer pixels only) ──────────────────────────────

def _place(sprite: np.ndarray, dx: int = 0, dy: int = 0) -> np.ndarray:
    frame = np.zeros((FRAME_H, FRAME_W, 4), np.uint8)
    h, w = sprite.shape[:2]
    x0, y0 = OFF_X - 1 + dx, OFF_Y - 1 + dy
    sx0, sy0 = max(0, -x0), max(0, -y0)
    fx0, fy0 = max(0, x0), max(0, y0)
    fx1, fy1 = min(FRAME_W, x0 + w), min(FRAME_H, y0 + h)
    region = sprite[sy0:sy0 + (fy1 - fy0), sx0:sx0 + (fx1 - fx0)]
    mask = region[..., 3] > 0
    frame[fy0:fy1, fx0:fx1][mask] = region[mask]
    return frame


def _breathe(sprite: np.ndarray, k: int, split: int) -> np.ndarray:
    """Shift everything above row `split` down by k (the body settles)."""

    if k == 0:
        return sprite
    out = sprite.copy()
    out[k:split + k] = sprite[0:split]
    out[0:k] = 0
    return out


def _squash(sprite: np.ndarray, rows: int) -> np.ndarray:
    """Drop `rows` evenly spaced rows (squash) or duplicate them (stretch, rows<0)."""

    h = sprite.shape[0]
    if rows == 0:
        return sprite
    if rows > 0:
        drop = set(np.linspace(h * 0.2, h * 0.8, rows, dtype=int).tolist())
        kept = [sprite[y] for y in range(h) if y not in drop]
        return np.concatenate([np.zeros((rows, sprite.shape[1], 4), np.uint8), np.stack(kept)])
    dup = set(np.linspace(h * 0.2, h * 0.8, -rows, dtype=int).tolist())
    rows_out = []
    for y in range(h):
        rows_out.append(sprite[y])
        if y in dup:
            rows_out.append(sprite[y])
    return np.stack(rows_out[-h:])


def _blink(sprite: np.ndarray, eyes: list, closed: float) -> np.ndarray:
    """Close each eye: the eye's own pixels (iris, white, lashes — anything
    that is not skin) take the cheek's colour, and a dark lid line is drawn
    across the columns that closed. `closed` is 0 (open) … 1 (shut)."""

    out = sprite.copy()
    # Skin is sampled between the eyes (the bridge of the nose): below an eye
    # can be hair or blush, between them is always face.
    lefts = sorted(eyes)
    bridge_x = (lefts[0][2] + lefts[-1][0]) // 2
    bridge_y = (lefts[0][1] + lefts[0][3]) // 2
    skin = out[bridge_y, bridge_x, :3].astype(int)
    for x0, y0, x1, y1 in eyes:
        rows = int(round((y1 - y0 + 1) * closed))
        if rows <= 0:
            continue
        box = out[y0:y0 + rows, x0:x1 + 1]
        distance = np.abs(box[..., :3].astype(int) - skin).sum(-1)
        eye = (box[..., 3] > 0) & (distance > 60)
        box[eye, :3] = skin
        columns = np.nonzero(eye.any(0))[0]
        if len(columns):
            lid = min(y0 + rows - 1, y1 - 1)
            out[lid, x0 + columns.min():x0 + columns.max() + 1] = OUTLINE
    return out


def _put(frame: np.ndarray, glyph: list[str], x: int, y: int, colours: dict[str, tuple]) -> None:
    for gy, row in enumerate(glyph):
        for gx, ch in enumerate(row):
            if ch == "." or ch == " ":
                continue
            fx, fy = x + gx, y + gy
            if 0 <= fx < FRAME_W and 0 <= fy < FRAME_H:
                frame[fy, fx] = colours[ch]


GLYPHS = {
    "z_small": ["ooo", ".o.", "ooo"],
    "z": ["oooo", "..o.", ".o..", "oooo"],
    "spark": [".s.", "scs", ".s."],
    "spark_big": ["..s..", "..s..", "sscss", "..s..", "..s.."],
    "tear": ["t", "t"],
    "drop": [".t.", "ttt", ".t."],
    "steam": [".dd.", "dssd", ".dd."],
    "steam_big": [".dd..", "dssdd", "dsssd", ".ddd."],
    "bang": ["a", "a", "a", ".", "a"],
}
GLYPH_COLOURS = {"o": FX["zzz"], "s": FX["spark"], "c": FX["spark_core"], "t": FX["tear"],
                 "d": FX["steam_dark"], "a": FX["alert"]}
GLYPH_COLOURS_STEAM = {"d": FX["steam_dark"], "s": FX["steam"]}


# ── animation recipes ───────────────────────────────────────────────────

BANDS = {"low": {"amp": 0, "speed": 0.75, "fx": 0}, "normal": {"amp": 1, "speed": 1.0, "fx": 1},
         "high": {"amp": 2, "speed": 1.25, "fx": 2}}


def _idle(name: str, sprite: np.ndarray, meta: dict, band: dict) -> list[tuple[np.ndarray, int]]:
    """6 frames; amplitude and particles by intensity band."""

    split = WAIST
    amp, fx = band["amp"], band["fx"]
    head = _head(sprite)                  # (x, y) in sprite pixels
    frames = []
    style = meta["idle"]
    for i in range(6):
        phase = [0, 0, 1, 1, 1, 0][i]
        s = _breathe(sprite, phase if style != "shake" else 0, split)
        dx = dy = 0
        if style == "bounce":
            dy = -[0, 1, 2, 2, 1, 0][i] * amp // 2 if amp else 0
            dy = -[0, 1, 2, 2, 1, 0][i] if amp == 2 else dy
        elif style == "sway":
            dy = -[0, 0, 1, 1, 0, 0][i] if amp else 0
        elif style == "shake":
            dx = [0, 1, 0, -1, 0, 1][i] if amp == 2 else ([0, 0, 1, 0, 0, -1][i] if amp == 1 else 0)
            s = _breathe(sprite, [0, 1, 1, 0, 1, 1][i], split)
        elif style == "droop":
            dy = [0, 0, 1, 1, 1, 0][i] if amp else 0
        elif style == "nod":
            s = _breathe(sprite, [0, 1, 2, 2, 1, 0][i] if amp else phase, split)
        elif style == "jitter":
            dy = -[0, 1, 0, 1, 0, 0][i] * max(1, amp) if amp else 0
        f = _place(s, dx, dy)
        hx, hy = OFF_X - 1 + head[0] + dx, OFF_Y - 1 + head[1] + dy
        if name == "sleepy":
            zs = [(hx - 14, hy + 2 - i), (hx - 18, hy - 6 - i)] if fx else [(hx - 14, hy + 2 - i // 2)]
            _put(f, GLYPHS["z_small"], zs[0][0], zs[0][1], GLYPH_COLOURS)
            if fx >= 1:
                _put(f, GLYPHS["z"], zs[1][0], zs[1][1], GLYPH_COLOURS)
            if fx == 2:
                _put(f, GLYPHS["z_small"], hx - 22, hy - 13 - i, GLYPH_COLOURS)
        elif name == "happy" and fx and i in (1, 2, 4):
            pts = [(hx - 16, hy + 8), (hx + 14, hy + 2), (hx + 18, hy + 16)]
            for k, (px, py) in enumerate(pts[: 1 + fx]):
                if (i + k) % 2:
                    _put(f, GLYPHS["spark_big" if fx == 2 and k == 0 else "spark"], px, py, GLYPH_COLOURS)
        elif name == "satisfied" and fx == 2 and i in (2, 3):
            _put(f, GLYPHS["spark"], hx + 16, hy + 4, GLYPH_COLOURS)
        elif name == "angry" and fx:
            # workload pressure: steam off the hood, a sweat drop — never at the user
            _put(f, GLYPHS["steam_big" if fx == 2 else "steam"], hx + 12 + (i % 2), hy - 2 - i // 2,
                 GLYPH_COLOURS_STEAM)
            if fx == 2:
                _put(f, GLYPHS["steam"], hx - 16 - (i % 2), hy + 1 - i // 2, GLYPH_COLOURS_STEAM)
            _put(f, GLYPHS["drop"], hx + 14, hy + 14 + (i // 3), GLYPH_COLOURS)
        elif name == "sad" and meta.get("tear"):
            tx, ty = meta["tear"]
            if fx or i >= 3:
                _put(f, GLYPHS["tear"], OFF_X - 1 + tx + dx, OFF_Y - 1 + ty + dy + (i % 3) * (1 + fx // 2),
                     GLYPH_COLOURS)
        elif name == "surprised" and fx and i in (0, 1, 2, 3):
            _put(f, GLYPHS["bang"], hx + 16, hy - 1 - (1 if i in (1, 2) else 0), GLYPH_COLOURS)
        frames.append((f, round(meta["frame_ms"] / band["speed"])))
    # occasional blink on the last-but-one frame for open-eyed states
    if meta.get("eyes"):
        f, ms = frames[4]
        s = _blink(_breathe(sprite, 1 if style != "shake" else 1, split), meta["eyes"], 1.0)
        frames[4] = (_place(s, 0, -([0, 1, 2, 2, 1, 0][4]) if (style == "bounce" and band["amp"] == 2) else 0), 110)
        frames.insert(5, (frames[3][0], 110))
    return frames


def _transition(sprite: np.ndarray) -> list[tuple[np.ndarray, int]]:
    return [(_place(_squash(sprite, 3)), 70), (_place(_squash(sprite, -2), 0, -1), 80), (_place(sprite), 90)]


def _long_idle(name: str, sprite: np.ndarray, meta: dict) -> list[tuple[np.ndarray, int]]:
    """Once, after ten minutes without a change: a stretch-and-settle."""

    split = WAIST
    frames = [(_place(sprite), 300), (_place(_squash(sprite, -2), 0, -1), 260), (_place(_squash(sprite, -3), 0, -2), 420),
              (_place(_squash(sprite, -2), 0, -1), 200), (_place(_squash(sprite, 2)), 160),
              (_place(_breathe(sprite, 1, split)), 260), (_place(sprite), 400)]
    if meta.get("eyes"):
        frames.insert(5, (_place(_blink(sprite, meta["eyes"], 1.0)), 120))
        frames.insert(6, (_place(sprite), 160))
        frames.insert(7, (_place(_blink(sprite, meta["eyes"], 1.0)), 120))
    return frames


# ── atlas ───────────────────────────────────────────────────────────────

def build(rig: dict) -> tuple[Image.Image, dict]:
    raw = {e: _base(e, rig) for e in EXPRESSIONS}
    quantised, palette = _quantise(raw)
    sprites = {e: _outline(_patch(q, rig["expressions"][e].get("patch", []))) for e, q in quantised.items()}

    unique: dict[bytes, str] = {}
    frames: list[np.ndarray] = []
    animations: dict[str, dict] = {}

    def add(anim: str, seq: list[tuple[np.ndarray, int]], loop: bool) -> None:
        names, durations = [], []
        for f, ms in seq:
            key = f.tobytes()
            if key not in unique:
                unique[key] = f"f{len(frames):03d}"
                frames.append(f)
            names.append(unique[key])
            durations.append(int(ms))
        animations[anim] = {"frames": names, "durations_ms": durations, "loop": loop}

    for e in EXPRESSIONS:
        meta = rig["expressions"][e]
        s = sprites[e]
        add(f"static_{e}", [(_place(s), 1000)], False)
        for band, params in BANDS.items():
            suffix = "" if band == "normal" else f"_{band}"
            add(f"idle_{e}{suffix}", _idle(e, s, meta, params), True)
        add(f"to_{e}", _transition(s), False)
        add(f"long_idle_{e}", _long_idle(e, s, meta), False)

    cols = 8
    rows = (len(frames) + cols - 1) // cols
    atlas = np.zeros((rows * FRAME_H, cols * FRAME_W, 4), np.uint8)
    index = {}
    for i, f in enumerate(frames):
        x, y = (i % cols) * FRAME_W, (i // cols) * FRAME_H
        atlas[y:y + FRAME_H, x:x + FRAME_W] = f
        index[f"f{i:03d}"] = {"x": x, "y": y}
    image = Image.fromarray(atlas, "RGBA")
    meta = {
        "schema_version": 1,
        "generator": "desktop/art/generate_sprites.py",
        "source": "assets/character/default (owner-approved originals, OD-CHAR-1; pixel adaptation OD-DESK-2)",
        "image": ATLAS_PNG.name,
        "image_size": [image.width, image.height],
        "frame_size": [FRAME_W, FRAME_H],
        "sprite_box": [OFF_X, OFF_Y, SPRITE_W, SPRITE_H],
        "expressions": EXPRESSIONS,
        "bands": {"low": [1, 3], "normal": [4, 7], "high": [8, 10]},
        "palette": ["#%02x%02x%02x" % c for c in palette] + ["#%02x%02x%02x" % v[:3] for v in FX.values()]
                   + ["#%02x%02x%02x" % OUTLINE[:3]],
        "frames": index,
        "animations": animations,
    }
    return image, meta


def _png_bytes(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, format="PNG", optimize=False, compress_level=9)
    return buf.getvalue()


def _json_bytes(meta: dict) -> bytes:
    return (json.dumps(meta, indent=1, sort_keys=True) + "\n").encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--check", action="store_true", help="regenerate in memory and compare with the committed files")
    args = parser.parse_args()
    rig = json.loads(RIG.read_text(encoding="utf-8"))
    image, meta = build(rig)
    if args.check:
        committed = np.array(Image.open(ATLAS_PNG).convert("RGBA"))
        fresh = np.array(image)
        same_pixels = committed.shape == fresh.shape and bool((committed == fresh).all())
        same_index = json.loads(ATLAS_JSON.read_text(encoding="utf-8")) == json.loads(_json_bytes(meta))
        if not (same_pixels and same_index):
            print(f"sprite atlas drift: pixels {'ok' if same_pixels else 'DIFFER'}, "
                  f"index {'ok' if same_index else 'DIFFERS'} — run desktop/art/generate_sprites.py")
            return 1
        print("sprite atlas reproduces exactly")
        return 0
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    png, js = _png_bytes(image), _json_bytes(meta)
    ATLAS_PNG.write_bytes(png)
    ATLAS_JSON.write_bytes(js)
    SUMS.write_text(
        f"{hashlib.sha256(png).hexdigest()}  desktop/src/sprites/{ATLAS_PNG.name}\n"
        f"{hashlib.sha256(js).hexdigest()}  desktop/src/sprites/{ATLAS_JSON.name}\n",
        encoding="utf-8",
    )
    print(f"{len(meta['frames'])} frames, {len(meta['animations'])} animations, atlas {image.width}x{image.height}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
