"""Retake the README's screenshots from the demo shelf.

    .venv/bin/pip install playwright && .venv/bin/playwright install chromium
    .venv/bin/python scripts/shots.py             # rewrites docs/img/*.png
    .venv/bin/python scripts/shots.py item stats  # only those

Playwright is not in requirements-dev.txt: nothing but this script wants it.

A demo shelf (scripts/demo.py) is built in a temp directory and served on a
free port with every key blanked, so a developer's own .env reaches nothing;
the pages are taken in the dark scheme. `identify.png` is the add form after
Identify: a stub worker on a second port answers with `CANNED`, a made-up
answer in the shape the schema asks for, for an invented case, so no model is
called. That shot is composed: the front photo that was sent sits beside the
form, and the form's Photos fieldset is hidden to keep the doubts and the
filled fields in frame.

The README lays the four page shots out as a grid, a wide one and a tall one to
a row, at percentage widths so a row never wraps. That holds only while the two
wide shots share one shape and the two tall ones another: change a size in
`SHOTS` and its partner has to follow, or the rows stop matching.
"""

import argparse
import base64
import json
import os
import random
import socket
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

import demo  # noqa: E402

OUT = ROOT / "docs" / "img"
ITEM = "A Map of Small Rooms"  # the most valuable: a box set with a few fetches behind it

# name: path, viewport width and height in CSS px, device scale
SHOTS = {
    "shelf": ("/?sort=format", 1132, 1020, 1),
    "phone": ("/", 390, 844, 2),
    "item": ("/items/{item}", 572, 1238, 2),
    "stats": ("/stats", 960, 865, 1),
}

# What the stub worker answers: identify.SCHEMA's fields, for a case that does not exist.
CANNED = {
    "title": "Kestrel Road", "format": "4K UHD", "edition": "Steelbook (Limited, #0412/2000)",
    "retailer": "Zavvi", "region": "UK", "upc": "", "condition": "sealed", "year": "1987",
    "edition_keywords": "Zavvi, 2000", "search_query": "",
    "doubts": [
        "retailer: the exclusive sticker is torn; Zavvi from the spine's logo",
        "upc: no barcode on the case or its sticker",
    ],
}
CANNED_COLOUR = (38, 58, 48)

# Every key the app reads, blanked: the environment outranks .env.
NO_KEYS = {k: "" for k in (
    "EBAY_CLIENT_ID", "EBAY_CLIENT_SECRET", "SERPAPI_KEY", "SOLDCOMPS_KEY",
    "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TMDB_API_KEY",
)} | {"AI_PROVIDER": "anthropic", "SOLD_LOOKUP_ENABLED": "false", "JUDGE_LISTINGS": "false",
      "REPRICE_ENABLED": "false", "ADMIN_USER": "demo", "ADMIN_PASSWORD": "demo"}

# identify.png: the page moves right, and the photo and an arrow take the space.
MAKE_ROOM = """() => {
  document.querySelector("form fieldset").style.display = "none";
  document.body.style.margin = "0 20px 0 620px";
}"""
COMPOSE = """([photo, height]) => {
  const side = document.createElement("div");
  side.style.cssText = `position:absolute; left:40px; top:${height / 2 - 240}px; width:545px;
    display:flex; align-items:center; gap:24px; color:var(--muted)`;
  side.innerHTML = `<figure style="margin:0; text-align:center; font-size:24px">
      <img src="${photo}" width="440" height="440" style="display:block; border-radius:18px">
      <figcaption style="margin-top:12px">the front photo</figcaption></figure>
    <svg width="81" height="34" viewBox="0 0 81 34" style="margin-bottom:44px">
      <path d="M0 17h58" stroke="var(--accent)" stroke-width="6"/>
      <path d="M56 0l25 17-25 17z" fill="var(--accent)"/></svg>`;
  document.body.append(side);
}"""


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class StubWorker(BaseHTTPRequestHandler):
    """tools/worker.py's /identify, answering CANNED whatever the photos are."""

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length", 0)))
        body = json.dumps(CANNED).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def serve(shelf: Path, port: int, worker_port: int) -> subprocess.Popen:
    env = os.environ | NO_KEYS | {
        "DATABASE_PATH": str(shelf / "steelshelf.db"), "PHOTO_DIR": str(shelf / "photos"),
        "WORKER_URL": f"http://127.0.0.1:{worker_port}", "WORKER_SECRET": "stub",
    }
    app = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port),
         "--log-level", "warning"],
        cwd=ROOT, env=env,
    )
    for _ in range(100):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=1)
            return app
        except OSError:
            if app.poll() is not None:
                break
            time.sleep(0.1)
    app.terminate()
    sys.exit("the app did not start")


def page_shot(browser, url: str, out: Path, width: int, height: int, scale: int) -> None:
    ctx = browser.new_context(
        viewport={"width": width, "height": height}, device_scale_factor=scale,
        color_scheme="dark", is_mobile=width < 500, has_touch=width < 500,
    )
    page = ctx.new_page()
    page.goto(url, wait_until="networkidle")
    page.evaluate("document.fonts.ready")
    page.screenshot(path=out)
    ctx.close()


def identify_shot(browser, base: str, out: Path) -> None:
    photo = demo.front(CANNED["title"], CANNED["format"], CANNED_COLOUR, random.Random(3))
    ctx = browser.new_context(
        viewport={"width": 1600, "height": 1200}, color_scheme="dark",
        http_credentials={"username": "demo", "password": "demo"},
    )
    page = ctx.new_page()
    page.goto(f"{base}/add", wait_until="networkidle")
    page.set_input_files("#front", {"name": "front.jpg", "mimeType": "image/jpeg",
                                    "buffer": photo})
    page.click("#identify")
    page.wait_for_selector(".doubts")
    page.evaluate("document.fonts.ready")
    # down to the retailer: the doubts are about it and the UPC, and the rest is scroll
    page.evaluate(MAKE_ROOM)
    box = page.locator("#retailer").bounding_box()
    height = round(box["y"] + box["height"] + 8)
    page.evaluate(COMPOSE, ["data:image/jpeg;base64," + base64.b64encode(photo).decode(), height])
    page.screenshot(path=out, clip={"x": 0, "y": 0, "width": 1600, "height": height})
    ctx.close()


def main() -> None:
    names = [*SHOTS, "identify"]
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("shots", nargs="*", metavar="shot",
                    help=f"which of {', '.join(names)} (default: all)")
    ap.add_argument("--out", default=str(OUT), help="where to write them (default docs/img)")
    args = ap.parse_args()
    if unknown := set(args.shots) - set(names):
        ap.error(f"no such shot: {', '.join(sorted(unknown))}")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        sys.exit("playwright is missing: .venv/bin/pip install playwright && "
                 ".venv/bin/playwright install chromium")
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmp:
        shelf = Path(tmp) / "demo"
        demo.build(shelf)
        with sqlite3.connect(shelf / "steelshelf.db") as conn:
            (item,) = conn.execute("SELECT id FROM items WHERE title = ?", (ITEM,)).fetchone()
        worker = HTTPServer(("127.0.0.1", 0), StubWorker)
        threading.Thread(target=worker.serve_forever, daemon=True).start()
        port = free_port()
        app = serve(shelf, port, worker.server_port)
        base = f"http://127.0.0.1:{port}"
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch()
                for name in args.shots or names:
                    if name == "identify":
                        identify_shot(browser, base, out / "identify.png")
                    else:
                        path, width, height, scale = SHOTS[name]
                        page_shot(browser, base + path.format(item=item), out / f"{name}.png",
                                  width, height, scale)
                    print(out / f"{name}.png")
                browser.close()
        finally:
            app.terminate()
            app.wait()
            worker.shutdown()


if __name__ == "__main__":
    main()
