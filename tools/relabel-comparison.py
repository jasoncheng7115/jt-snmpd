#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Redraw the header bars on the side-by-side comparison screenshots.

Each comparison image stacks two LibreNMS pages, one from a host running the
built-in Windows SNMP Service and one from a host running jt-snmpd, with a
coloured bar above each saying which is which. The bars were composited by hand
originally, and they were too short to read comfortably at page width.

They also said only "Windows 10 22H2 physical machine", which left the obvious
question unanswered and invited a wrong one. **The two hosts are different
machines**, and their absolute figures differ for that reason alone: 10 GiB of
memory against 16. The comparison is about which tables LibreNMS receives, not
about the numbers matching, so each bar now names its own hardware.

The bar for the built-in service said "physical machine" and that was wrong: it
is a QEMU/KVM virtual machine. The label has to be right even though it invites
the objection that a virtual machine has no hardware to report, because the
objection has an answer — another QEMU virtual machine running jt-snmpd reports
a full entPhysical table. Writing "physical" to dodge the question would have
been trading a real answer for a false premise.

The bars are found by colour rather than by position, because the two pages have
different heights in each image, and replaced in place. Running this twice is
harmless: it finds the bars it drew last time and draws them again.

Hardware facts come from LibreNMS's own record of the two devices:

    jt-snmpd host   entPhysical, parsed from SMBIOS by the agent itself:
                    Dell Latitude E5270, Core i5-6300U, 16 GB DDR4-2133
    built-in host   sysDescr only, because the built-in service publishes no
                    entPhysical at all: a QEMU/KVM guest on an AMD Ryzen host,
                    8 vCPUs, 10 GiB, Windows 10 22H2

Usage::

    python3 tools/relabel-comparison.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
IMAGES = ROOT / "docs" / "images"

GREY = (108, 117, 125)
GREEN = (25, 118, 96)

# Noto Sans CJK carries both the Latin and the Traditional Chinese glyphs, so one
# face covers both language variants and they stay visually consistent.
FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_TC_INDEX = 3            # 0=JP 1=KR 2=SC 3=TC 4=HK

BAR_HEIGHT = 104             # was 64, which was hard to read at page width
FONT_SIZE = 46
PAD_LEFT = 26

LABELS = {
    # colour -> (Traditional Chinese, English)
    GREY: ("Windows 內建 SNMP Service　·　QEMU 虛擬機　·　Windows 10 22H2",
           "Built-in SNMP Service  ·  QEMU virtual machine  ·  Windows 10 22H2"),
    GREEN: ("jt-snmpd　·　Dell Latitude E5270　·　Windows 10 22H2",
            "jt-snmpd  ·  Dell Latitude E5270  ·  Windows 10 22H2"),
}


def find_bars(img: Image.Image, colour: tuple, tol: int = 10) -> list[tuple[int, int]]:
    """Rows where most of the width is the bar colour.

    Sampled rather than scanned in full: the bars run the whole width, so a
    sample every 70 px is enough and keeps this quick on a 2804-wide image.
    """
    w, h = img.size
    px = img.load()
    out: list[tuple[int, int]] = []
    start = None
    for y in range(h):
        row = [px[x, y] for x in range(30, w - 30, 70)]
        near = sum(1 for c in row
                   if max(abs(a - b) for a, b in zip(c, colour)) <= tol)
        is_bar = near >= len(row) * 0.6
        if is_bar and start is None:
            start = y
        elif not is_bar and start is not None:
            if y - start > 20:
                out.append((start, y - 1))
            start = None
    if start is not None and h - start > 20:
        out.append((start, h - 1))
    return out


def draw_bar(width: int, colour: tuple, text: str, font: ImageFont.FreeTypeFont) -> Image.Image:
    bar = Image.new("RGB", (width, BAR_HEIGHT), colour)
    d = ImageDraw.Draw(bar)
    box = font.getbbox(text)
    y = (BAR_HEIGHT - (box[3] - box[1])) // 2 - box[1]
    d.text((PAD_LEFT, y), text, font=font, fill=(255, 255, 255))
    return bar


def relabel(path: Path, font: ImageFont.FreeTypeFont, english: bool) -> str:
    img = Image.open(path).convert("RGB")
    found = []
    for colour in (GREY, GREEN):
        for a, b in find_bars(img, colour):
            found.append((a, b, colour))
    if len(found) != 2:
        return f"expected two bars, found {len(found)}; left alone"
    found.sort()

    # Rebuild top to bottom, substituting each bar with a taller one.
    pieces: list[Image.Image] = []
    cursor = 0
    for top, bottom, colour in found:
        pieces.append(img.crop((0, cursor, img.width, top)))
        pieces.append(draw_bar(img.width, colour, LABELS[colour][1 if english else 0], font))
        cursor = bottom + 1
    pieces.append(img.crop((0, cursor, img.width, img.height)))

    out = Image.new("RGB", (img.width, sum(p.height for p in pieces)), (255, 255, 255))
    y = 0
    for p in pieces:
        out.paste(p, (0, y))
        y += p.height
    out.save(path, "PNG", optimize=True)
    return f"{img.height} -> {out.height}"


def main() -> int:
    if not Path(FONT_PATH).exists():
        print(f"missing {FONT_PATH}", file=sys.stderr)
        return 1
    font = ImageFont.truetype(FONT_PATH, FONT_SIZE, index=FONT_TC_INDEX)
    targets = sorted(p for p in IMAGES.glob("*.png")
                     if not p.name.startswith("install-") and "icon" not in p.name)
    if not targets:
        print("no comparison images found", file=sys.stderr)
        return 1
    for p in targets:
        print(f"  {p.name:24} {relabel(p, font, p.stem.endswith('-en'))}")
    print("\nThese are images. Look at them, then run "
          "`python3 tools/check-privacy.py --update-images`.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
