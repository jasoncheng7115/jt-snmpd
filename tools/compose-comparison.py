#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Stack the built-in-service and jt-snmpd captures into one comparison figure.

Usage::

    python3 tools/compose-comparison.py <captures-dir> docs/images <en|zh-TW>

<captures-dir>/<locale>/ holds eight LibreNMS page captures named
<builtin|jtsnmpd>-<temperature|smart|ports|memory>.png.

Both halves now come from **the same machine**, a Dell Latitude E5270 running
Windows 10 22H2, captured before and after handing UDP/161 from one agent to the
other. The earlier pair used two different hosts, which meant every figure came
with a paragraph explaining why the absolute numbers differed. One machine
removes the question instead of answering it.

The ports figure is cropped at the MAC column. Everything to the right of it is
the discovered-neighbour list, which is an inventory of whatever else is on the
network -- device names of a domain controller, an IPMI interface, a router --
and none of it is part of what this figure is showing.

This replaced tools/relabel-comparison.py, which redrew the coloured bars on
finished images. That existed because the bars had been composited by hand and
the labels were wrong; drawing them here means there is one place where a label
can be wrong, and it is the place the image is made.
"""
import sys, pathlib
from PIL import Image, ImageDraw, ImageFont

SRC = pathlib.Path(sys.argv[1])
DST = pathlib.Path(sys.argv[2]); DST.mkdir(parents=True, exist_ok=True)
LOCALE = sys.argv[3]

FONT_PATH = "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc"
FONT_TC = 3
BAR, FONT_SIZE, PAD_LEFT = 104, 46, 26
GREY, GREEN, BG = (108, 117, 125), (25, 118, 96), (255, 255, 255)

LABELS = {
    "zh-TW": {
        "builtin": "Windows 內建 SNMP Service　·　Dell Latitude E5270　·　Windows 10 22H2",
        "jtsnmpd": "jt-snmpd　·　同一台機器　·　Windows 10 22H2",
    },
    "en": {
        "builtin": "Built-in SNMP Service  ·  Dell Latitude E5270  ·  Windows 10 22H2",
        "jtsnmpd": "jt-snmpd  ·  the same machine  ·  Windows 10 22H2",
    },
}

# name -> width to crop to, or None for the full capture
PAIRS = {"temperature": None, "smart": None, "ports": 2010, "memory": None}

font = ImageFont.truetype(FONT_PATH, FONT_SIZE, index=FONT_TC)


def bar(width, colour, text):
    im = Image.new("RGB", (width, BAR), colour)
    d = ImageDraw.Draw(im)
    box = font.getbbox(text)
    d.text((PAD_LEFT, (BAR - (box[3] - box[1])) // 2 - box[1]), text,
           font=font, fill=(255, 255, 255))
    return im


for name, cut in PAIRS.items():
    parts = []
    for tag, colour in (("builtin", GREY), ("jtsnmpd", GREEN)):
        f = SRC / LOCALE / f"{tag}-{name}.png"
        if not f.exists():
            print(f"  skipped {name}: {f.name} is missing"); parts = []; break
        im = Image.open(f).convert("RGB")
        if cut:
            im = im.crop((0, 0, min(cut, im.width), im.height))
        parts.append((colour, LABELS[LOCALE][tag], im))
    if not parts:
        continue

    width = max(im.width for _, _, im in parts)
    height = sum(BAR + im.height for _, _, im in parts)
    out = Image.new("RGB", (width, height), BG)
    y = 0
    for colour, text, im in parts:
        out.paste(bar(width, colour, text), (0, y)); y += BAR
        out.paste(im, (0, y)); y += im.height
    dst = DST / f"{name}-{LOCALE}.png"
    out.save(dst, "PNG", optimize=True)
    print(f"  {dst.name:26} {out.width}x{out.height}  {dst.stat().st_size//1024} KB")
