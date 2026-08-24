#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Generate the installer's wizard artwork and licence document.

**Why this exists**

0.9.3 shipped a wizard wearing WiX's stock placeholders: a red "no entry" banner
in the top-right of every page, WiX's default side panel on the welcome page,
and -- worst of the three -- a licence page containing *Lorem ipsum*. A
placeholder EULA is not a cosmetic problem. It is a document presented as terms
of use that says nothing, in an installer for software that is actually licensed
GPL-3.0-or-later.

The three files are generated from sources already in the repository, so they
cannot drift from the project's identity:

    docs/brand/icon-512.png  ->  banner.bmp, dialog.bmp
    LICENSE                  ->  license.rtf

The outputs are committed rather than built on the Windows machine, because the
build host has WiX but no Python imaging library, and because a wizard's
appearance should be reviewable in a diff rather than produced fresh on whatever
machine happened to run the build.

WiX expects specific sizes, and it does not scale:

    banner.bmp   493 x 58    across the top of every page after the first
    dialog.bmp   493 x 312   the welcome and exit pages; the dialog paints its
                             own white text panel over the right-hand portion,
                             so only the left ~165 px is ever seen

Usage::

    python3 packaging/make-ui-assets.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
BRAND = ROOT / "docs" / "brand" / "icon-512.png"
LICENSE = ROOT / "LICENSE"
OUT = ROOT / "packaging" / "wix"

# The project's accent, the same green as the icon and the documentation site.
ACCENT = (21, 128, 106)
ACCENT_DARK = (13, 94, 78)
WHITE = (255, 255, 255)

BANNER_SIZE = (493, 58)
DIALOG_SIZE = (493, 312)
# How much of the dialog bitmap the wizard actually leaves visible. The rest is
# covered by the text panel, so painting it white keeps the seam invisible.
DIALOG_VISIBLE_WIDTH = 165


def _vertical_gradient(size: tuple[int, int], top: tuple, bottom: tuple) -> Image.Image:
    """A flat colour looks cheap at this size; a slight gradient does not."""
    w, h = size
    img = Image.new("RGB", size, top)
    d = ImageDraw.Draw(img)
    for y in range(h):
        t = y / max(h - 1, 1)
        d.line([(0, y), (w, y)],
               fill=tuple(round(a + (b - a) * t) for a, b in zip(top, bottom)))
    return img


def _icon(px: int) -> Image.Image:
    """The brand icon at a given size, still carrying its alpha channel."""
    return Image.open(BRAND).convert("RGBA").resize((px, px), Image.LANCZOS)


def _glyph(px: int, colour: tuple = WHITE) -> Image.Image:
    """Just the mark from inside the icon tile, in a single flat colour.

    The icon is a white mark on a green rounded square. Pasting that tile onto
    the green side band puts green on green and the mark almost disappears.
    Lifting the mark out and drawing it directly gives the contrast the tile was
    providing in the first place.

    The mark is separated by luminance: it is the only near-white content in the
    image, and the tile behind it is well clear of that threshold.
    """
    src = Image.open(BRAND).convert("RGBA").resize((px, px), Image.LANCZOS)
    r, g, b, alpha = src.split()
    lum = Image.merge("RGB", (r, g, b)).convert("L")
    mask = lum.point(lambda v: 255 if v > 200 else 0)
    # Anywhere the tile is transparent is outside the icon and must stay out
    mask = Image.composite(mask, Image.new("L", src.size, 0),
                           alpha.point(lambda v: 255 if v > 128 else 0))
    out = Image.new("RGBA", src.size, colour + (0,))
    out.putalpha(mask)
    return out


def build_banner() -> Path:
    """Top banner: white, with the icon at the right.

    White rather than the accent colour: the wizard prints the page heading over
    the left of this strip in dark text, and a coloured background there would
    make the heading unreadable. Only the right edge is ours to use.
    """
    img = Image.new("RGB", BANNER_SIZE, WHITE)
    icon_px = 44
    icon = _icon(icon_px)
    img.paste(icon, (BANNER_SIZE[0] - icon_px - 10,
                     (BANNER_SIZE[1] - icon_px) // 2), icon)
    # A hairline in the accent colour along the bottom, to separate the banner
    # from the page body the way the rest of the project's surfaces do.
    ImageDraw.Draw(img).line([(0, BANNER_SIZE[1] - 1),
                              (BANNER_SIZE[0], BANNER_SIZE[1] - 1)], fill=ACCENT)
    out = OUT / "banner.bmp"
    img.save(out, "BMP")
    return out


def build_dialog() -> Path:
    """Welcome/exit side panel: an accent band on the left, white to its right.

    Only the left ~165 px is visible; the wizard covers the remainder with its
    own white panel. Painting that remainder white means no seam shows if a
    Windows release ever changes the panel's width slightly.
    """
    img = Image.new("RGB", DIALOG_SIZE, WHITE)
    band = _vertical_gradient((DIALOG_VISIBLE_WIDTH, DIALOG_SIZE[1]),
                              ACCENT, ACCENT_DARK)
    img.paste(band, (0, 0))
    glyph_px = 104
    glyph = _glyph(glyph_px)
    img.paste(glyph, ((DIALOG_VISIBLE_WIDTH - glyph_px) // 2, 84), glyph)
    out = OUT / "dialog.bmp"
    img.save(out, "BMP")
    return out


def _rtf_escape(text: str) -> str:
    """RTF reserves backslash and braces, and is a byte format.

    The GPL text is ASCII apart from typographic quotes, which are escaped to
    their code points so the file stays plain 7-bit and cannot be mangled by a
    build host's code page.
    """
    out = []
    for ch in text:
        if ch in "\\{}":
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\par\n")
        elif ord(ch) < 128:
            out.append(ch)
        else:
            out.append(f"\\u{ord(ch)}?")
    return "".join(out)


def build_license() -> Path:
    """Turn the repository's LICENSE into the RTF the licence page displays.

    Generated from LICENSE rather than kept as a second copy: two copies of a
    licence drift, and the one users agree to during installation should be the
    one in the repository, not a snapshot of it.
    """
    text = LICENSE.read_text(encoding="utf-8")
    body = _rtf_escape(text)
    rtf = (
        r"{\rtf1\ansi\ansicpg1252\deff0"
        r"{\fonttbl{\f0\fnil\fcharset0 Segoe UI;}{\f1\fmodern\fcharset0 Consolas;}}"
        r"\viewkind4\uc1\pard\f0\fs18 "
        + body +
        r"\par}"
    )
    out = OUT / "license.rtf"
    out.write_text(rtf, encoding="ascii")
    return out


def main() -> int:
    if not BRAND.exists():
        print(f"missing {BRAND}", file=sys.stderr)
        return 1
    if not LICENSE.exists():
        print(f"missing {LICENSE}", file=sys.stderr)
        return 1
    OUT.mkdir(parents=True, exist_ok=True)
    for f in (build_banner(), build_dialog(), build_license()):
        print(f"  {f.relative_to(ROOT)}  {f.stat().st_size:,} bytes")
    print("\nRemember: these are images. Run "
          "`python3 tools/check-privacy.py --update-images` after reviewing them.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
