#!/usr/bin/env python3
"""Build the README's horizontal lockups: mark.svg beside "steelshelf" in Fraunces 700, outlined.

    .venv/bin/pip install fonttools brotli uharfbuzz   # once; only this needs them
    .venv/bin/python scripts/render-lockup.py

Writes design/logo/steelshelf-horizontal-{colour,reversed}.svg (dark ink for light pages, pale
for dark) and steelshelf-banner.svg, the colour lockup on a pale blue-grey panel. The README uses
the banner: Gitea wraps a README <img> in a link, which breaks <picture>'s dark-mode <source>, so
the header has to read on any theme by itself. The text is paths, so no font is needed.
"""

import io
from pathlib import Path

import uharfbuzz as hb
from fontTools.pens.svgPathPen import SVGPathPen
from fontTools.pens.transformPen import TransformPen
from fontTools.ttLib import TTFont
from fontTools.varLib import instancer

REPO = Path(__file__).resolve().parent.parent
FONT = REPO / "app" / "static" / "fonts" / "fraunces-latin.woff2"
MARK = REPO / "app" / "static" / "mark.svg"
OUT = REPO / "design" / "logo"
INKS = {"colour": "#111A2C", "reversed": "#E6ECF5"}  # --fg in the light and dark themes

SCALE = 0.105  # font units to lockup units: Fraunces caps (1400) come out 147 against a 256 mark
GAP = 56  # between the tile and the wordmark
PAD = 40  # banner padding around the lockup
PANEL = "#EEF2F8"  # --bg in the light theme


def wordmark_path() -> tuple[str, float]:
    font = instancer.instantiateVariableFont(TTFont(FONT), {"wght": 700, "opsz": 72})
    font.flavor = None
    raw = io.BytesIO()
    font.save(raw)
    shaper = hb.Font(hb.Face(raw.getvalue()))
    buf = hb.Buffer()
    buf.add_str("steelshelf")
    buf.guess_segment_properties()
    hb.shape(shaper, buf, {"kern": True, "liga": False})

    glyphs, order = font.getGlyphSet(), font.getGlyphOrder()
    pen = SVGPathPen(glyphs)
    x0 = 256 + GAP
    baseline = 128 + 1400 * SCALE / 2 - 8  # caps centred on the tile, nudged up optically
    x = 0
    for info, pos in zip(buf.glyph_infos, buf.glyph_positions):
        move = (SCALE, 0, 0, -SCALE, x0 + (x + pos.x_offset) * SCALE, baseline)
        glyphs[order[info.codepoint]].draw(TransformPen(pen, move))
        x += pos.x_advance
    return pen.getCommands(), x0 + x * SCALE + 8


def main() -> None:
    d, width = wordmark_path()
    width = round(width)
    tile = MARK.read_text().split("-->", 1)[1].rsplit("</svg>", 1)[0].strip()
    OUT.mkdir(parents=True, exist_ok=True)
    for variant, ink in INKS.items():
        svg = (
            f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {width} 256" width="{width}"'
            f' height="256" role="img" aria-label="steelshelf"><title>steelshelf</title>\n'
            f'{tile}\n<path fill="{ink}" d="{d}"/>\n</svg>\n'
        )
        (OUT / f"steelshelf-horizontal-{variant}.svg").write_text(svg)
    w, h = width + 2 * PAD, 256 + 2 * PAD
    banner = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {w} {h}" width="{w}" height="{h}"'
        f' role="img" aria-label="steelshelf"><title>steelshelf</title>\n'
        f'<rect width="{w}" height="{h}" rx="48" fill="{PANEL}"/>\n'
        f'<g transform="translate({PAD} {PAD})">\n{tile}\n<path fill="{INKS["colour"]}" d="{d}"/>\n</g>\n</svg>\n'
    )
    (OUT / "steelshelf-banner.svg").write_text(banner)
    print("wrote design/logo/steelshelf-{horizontal-colour,horizontal-reversed,banner}.svg")


if __name__ == "__main__":
    main()
