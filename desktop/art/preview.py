#!/usr/bin/env python3
"""Render previews of the desktop companion's sprites from the pinned atlas:

    docs/images/desktop/expressions.png   — the seven expressions, 3x, static
    docs/images/desktop/idle.gif          — all seven idling (normal band), 2x
    docs/images/desktop/bands.png         — angry and happy across low/normal/high

Reads only desktop/src/sprites/jarvis-atlas.{png,json}. Requires Pillow.
"""
import json
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parents[2]
META = json.loads((ROOT / "desktop/src/sprites/jarvis-atlas.json").read_text())
ATLAS = Image.open(ROOT / "desktop/src/sprites/jarvis-atlas.png").convert("RGBA")
OUT = ROOT / "docs/images/desktop"
FW, FH = META["frame_size"]
S = 3
BG = (54, 62, 78, 255)


def frame(name: str, scale: int = S) -> Image.Image:
    f = META["frames"][name]
    return ATLAS.crop((f["x"], f["y"], f["x"] + FW, f["y"] + FH)).resize((FW * scale, FH * scale), Image.NEAREST)


def labelled_row(items, scale: int = S):
    pad, label_h = 8, 18
    img = Image.new("RGBA", (len(items) * (FW * scale + pad) + pad, FH * scale + label_h + pad), BG)
    d = ImageDraw.Draw(img)
    for i, (label, im) in enumerate(items):
        x = pad + i * (FW * scale + pad)
        img.alpha_composite(im, (x, 0))
        d.text((x + 4, FH * scale + 2), label, fill=(235, 238, 245, 255))
    return img


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    exprs = META["expressions"]
    labelled_row([(e, frame(META["animations"][f"static_{e}"]["frames"][0])) for e in exprs]).save(OUT / "expressions.png")

    rows = []
    for e in ("happy", "angry", "sleepy"):
        rows.append(labelled_row([(f"{e} {band}", frame(META["animations"][f"idle_{e}{suffix}"]["frames"][1]))
                                  for band, suffix in (("low", "_low"), ("normal", ""), ("high", "_high"))]))
    sheet = Image.new("RGBA", (rows[0].width, sum(r.height for r in rows)), BG)
    y = 0
    for r in rows:
        sheet.alpha_composite(r, (0, y))
        y += r.height
    sheet.save(OUT / "bands.png")

    # 100 ms ticks; each expression advances by its own frame durations.
    ticks, frames = 30, []
    for t in range(ticks):
        now = t * 100
        items = []
        for e in exprs:
            anim = META["animations"][f"idle_{e}"]
            total = sum(anim["durations_ms"])
            at, acc, idx = now % total, 0, 0
            for i, d in enumerate(anim["durations_ms"]):
                acc += d
                if at < acc:
                    idx = i
                    break
            items.append((e, frame(anim["frames"][idx], 2)))
        frames.append(labelled_row(items, 2).convert("P", palette=Image.ADAPTIVE, colors=64))
    frames[0].save(OUT / "idle.gif", save_all=True, append_images=frames[1:], duration=100, loop=0, disposal=2)


if __name__ == "__main__":
    main()
