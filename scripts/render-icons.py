#!/usr/bin/env python3
"""Render steelshelf's PNG and ICO icons from their SVG masters.

    .venv/bin/pip install cairosvg pillow   # once; not in requirements, only this needs them
    .venv/bin/python scripts/render-icons.py

Masters:
  app/static/mark.svg       the full mark: header, manifest SVG, 192/512 PWA icons
  app/static/icon.svg       the small-size cut: browser tab and favicon.ico
  design/icon/touch.svg     full-bleed square; iOS rounds the corners itself
  design/icon/maskable.svg  full-bleed, mark inside Android's 80% safe circle
"""

import io
from pathlib import Path

import cairosvg
from PIL import Image

REPO = Path(__file__).resolve().parent.parent
STATIC = REPO / "app" / "static"
DESIGN = REPO / "design" / "icon"


def render(svg: Path, size: int) -> Image.Image:
    png = cairosvg.svg2png(url=str(svg), output_width=size, output_height=size)
    return Image.open(io.BytesIO(png)).convert("RGBA")


def main() -> None:
    render(STATIC / "mark.svg", 192).save(STATIC / "icon-192.png", optimize=True)
    render(STATIC / "mark.svg", 512).save(STATIC / "icon-512.png", optimize=True)
    render(DESIGN / "maskable.svg", 512).save(STATIC / "icon-maskable-512.png", optimize=True)
    touch = render(DESIGN / "touch.svg", 180).convert("RGB")
    touch.save(STATIC / "apple-touch-icon.png", optimize=True)
    small = STATIC / "icon.svg"
    render(small, 32).save(
        STATIC / "favicon.ico", sizes=[(16, 16), (32, 32)], append_images=[render(small, 16)]
    )
    print("wrote icon-192, icon-512, icon-maskable-512, apple-touch-icon, favicon.ico")


if __name__ == "__main__":
    main()
