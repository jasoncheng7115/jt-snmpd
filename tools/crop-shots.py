#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Trim installer screenshots and normalise them for the documentation site.

A screenshot of a wizard arrives carrying whatever surrounded it: a desktop, a
file manager, a browser address bar naming an internal host, a taskbar. None of
that belongs on a public page, and it is exactly the kind of disclosure
`tools/check-privacy.py` cannot catch, because it is pixels.

**Capture the window, not the screen.** `Alt+PrtScn` copies only the active
window, so there is nothing to detect and nothing to guess at: the image is the
dialog. This tool then trims the drop shadow and normalises the width.

A detector was tried first and is deliberately not here. Finding "the bright
rectangle" fails on this project's own wizard, whose welcome page is half a dark
green band -- the detector clipped the dialog to the white half and would have
done it silently. A crop that is confidently wrong is worse than a tool that
refuses, so a full-screen capture is reported and skipped rather than guessed
at.

Nothing inside the dialog is retouched. If a field holds a real network or a
real community string, the fix is to take the screenshot again with
documentation values, not to paint over the evidence.

Usage::

    python3 tools/crop-shots.py <in-dir> <out-dir> [--width 900]
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from PIL import Image, ImageChops

# Above this, the capture is a screen rather than a window. The wizard is 493
# dialog units wide, which is around 500 px at 100% DPI and around 1000 px at
# 200%; a capture wider than this is showing more than the window.
MAX_WINDOW_WIDTH = 1100


def trim_uniform_border(img: Image.Image) -> Image.Image:
    """Remove a uniform border, which is usually the window's drop shadow.

    The reference colour is the top-left pixel. If the image has no such border
    the bounding box is the whole image and nothing changes.
    """
    bg = Image.new("RGB", img.size, img.getpixel((0, 0)))
    diff = ImageChops.difference(img, bg).convert("L").point(lambda v: 255 if v > 12 else 0)
    box = diff.getbbox()
    return img.crop(box) if box else img


def process(src: Path, dst: Path, width: int) -> str:
    img = Image.open(src).convert("RGB")
    if img.width > MAX_WINDOW_WIDTH:
        return (f"SKIPPED: {img.width}x{img.height} is a screen capture, not a window. "
                "Retake it with Alt+PrtScn so only the dialog is captured")
    out = trim_uniform_border(img)
    note = f"{img.width}x{img.height}"
    if out.size != img.size:
        note += f" -> trimmed {out.width}x{out.height}"
    if out.width != width:
        out = out.resize((width, round(out.height * width / out.width)), Image.LANCZOS)
        note += f" -> {out.width}x{out.height}"
    dst.parent.mkdir(parents=True, exist_ok=True)
    out.save(dst, "PNG", optimize=True)
    return note


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Trim installer screenshots and normalise them for the site")
    ap.add_argument("indir", type=Path)
    ap.add_argument("outdir", type=Path)
    ap.add_argument("--width", type=int, default=900,
                    help="output width in pixels (default 900)")
    args = ap.parse_args()

    files = sorted(p for p in args.indir.iterdir()
                   if p.suffix.lower() in (".png", ".jpg", ".jpeg", ".bmp"))
    if not files:
        print(f"no images in {args.indir}", file=sys.stderr)
        return 1
    skipped = 0
    for f in files:
        note = process(f, args.outdir / (f.stem + ".png"), args.width)
        print(f"  {f.name:36} {note}")
        skipped += note.startswith("SKIPPED")
    print("\nLook at every one of them before they go anywhere near docs/: "
          "check for MAC addresses, internal ranges, real community strings and "
          "neighbour host names. A regular expression cannot read pixels.")
    return 1 if skipped else 0


if __name__ == "__main__":
    raise SystemExit(main())
