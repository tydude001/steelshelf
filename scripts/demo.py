"""Build a demo shelf: invented steelbooks, drawn photos, made-up price history.

    .venv/bin/python scripts/demo.py            # writes ./demo/ (git-ignored)
    .venv/bin/python scripts/demo.py --force    # replaces an existing ./demo/

then run the app against it, no keys needed:

    DATABASE_PATH=demo/steelshelf.db PHOTO_DIR=demo/photos REPRICE_ENABLED=false \\
        .venv/bin/uvicorn app.main:app --port 8010

Every title, edition and price here is made up, and every photo is drawn: a
flat-colour case in the middle of a table-coloured frame, where `photos.CASE_BOX`
expects a real case to be, with a spine band down its left edge so the shelf's
front-edge strip reads as a spine. README screenshots come from this, never
from a real shelf: real steelbook art belongs to the studios and artists.
"""

import argparse
import io
import random
import shutil
import sys
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import db, photos  # noqa: E402

FRAME = (1200, 1200)
TABLE = (58, 52, 47)

# title, format, edition, retailer, region, genre, director, colour, paid, ask
CASES = [
    ("Neon Harbor", "4K UHD", "Steelbook", "Best Buy", "US", "Science Fiction",
     "Mara Quill", (22, 38, 92), 34.99, 48.0),
    ("The Glass Orchard", "Blu-ray", "Steelbook (Limited)", "Zavvi", "UK", "Drama",
     "Otto Brenner", (140, 32, 44), 24.99, 61.0),
    ("Low Tide Protocol", "4K+BD", "Steelbook", "Walmart", "US", "Thriller",
     "Ines Carvalho", (18, 90, 84), 29.99, 27.0),
    ("Salt & Static", "4K UHD", "Steelbook", "Target", "US", "Science Fiction",
     "Mara Quill", (208, 132, 30), 39.99, 55.0),
    ("Nightjar", "Blu-ray", "Steelbook", "HMV", "UK", "Horror",
     "Theo Vance", (30, 30, 34), 19.99, 22.0),
    ("A Map of Small Rooms", "4K UHD", "Steelbook (Box set)", "Zavvi", "UK", "Drama",
     "Otto Brenner", (212, 196, 160), 59.99, 95.0),
    ("Copperline", "4K+BD", "Steelbook", "Best Buy", "US", "Western",
     "Ruth Okafor", (150, 84, 40), 32.99, 30.0),
    ("The Quiet Engine", "4K UHD", "Steelbook", "Amazon", "DE", "Science Fiction",
     "Ines Carvalho", (70, 76, 88), 44.99, 52.0),
    ("Moth Season", "Blu-ray", "Steelbook", "Walmart", "US", "Horror",
     "Theo Vance", (86, 40, 110), 21.99, 38.0),
    ("Parade of Lanterns", "4K UHD", "Steelbook (Limited)", "Best Buy", "US", "Animation",
     "Kenji Arai & Lisa Moreau", (200, 60, 40), 36.99, 74.0),
    ("Undertow", "4K+BD", "Steelbook", "Zavvi", "UK", "Thriller",
     "Ruth Okafor", (16, 60, 110), 27.99, 26.0),
    ("Blue Hour Heist", "4K UHD", "Steelbook", "Target", "US", "Crime",
     "Mara Quill", (40, 120, 170), 34.99, 41.0),
    ("Ferrous", "4K UHD", "Steelbook (Limited)", "Zavvi", "UK", "Science Fiction",
     "Ines Carvalho", (120, 124, 130), 42.99, 88.0),
    ("The Lighthouse Ledger", "Blu-ray", "Steelbook", "HMV", "UK", "Mystery",
     "Otto Brenner", (30, 70, 60), 17.99, 19.0),
    ("Paper Kites", "4K+BD", "Steelbook", "Best Buy", "US", "Animation",
     "Kenji Arai", (230, 180, 60), 29.99, 35.0),
    ("Cold Open", "4K UHD", "Steelbook", "Walmart", "US", "Comedy",
     "Lisa Moreau", (180, 40, 90), 24.99, 23.0),
    ("Red Meridian", "4K UHD", "Steelbook", "Amazon", "FR", "Western",
     "Ruth Okafor", (120, 30, 20), 39.99, 47.0),
    ("Signal Fire", "Blu-ray", "Steelbook", "Target", "US", "Thriller",
     "Theo Vance", (60, 20, 30), 19.99, 21.0),
    ("Orchid Motel", "4K UHD", "Steelbook (Limited)", "Best Buy", "US", "Crime",
     "Mara Quill", (190, 110, 150), 34.99, 66.0),
    ("Wintering", "4K+BD", "Steelbook", "Zavvi", "UK", "Drama",
     "Otto Brenner", (170, 190, 200), 32.99, 34.0),
]


def font(size: int) -> ImageFont.ImageFont:
    return ImageFont.load_default(size)


def ink(bg: tuple[int, int, int]) -> tuple[int, int, int]:
    return (20, 20, 22) if sum(bg) > 420 else (240, 236, 228)


def shade(c: tuple[int, int, int], f: float) -> tuple[int, int, int]:
    return tuple(max(0, min(255, int(v * f))) for v in c)


def case_box() -> tuple[int, int, int, int]:
    w, h = FRAME
    x0, y0, x1, y1 = photos.CASE_BOX
    return int(w * x0), int(h * y0), int(w * x1), int(h * y1)


def jpeg(img: Image.Image) -> bytes:
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88)
    return out.getvalue()


def front(title: str, fmt: str, colour: tuple[int, int, int], rng: random.Random) -> bytes:
    img = Image.new("RGB", FRAME, TABLE)
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = case_box()
    d.rectangle((x0, y0, x1, y1), fill=colour)
    # the spine band the shelf's front-edge strip cuts: SPINE_STRIP's aspect
    band = int((y1 - y0) * photos.SPINE_STRIP[0] / photos.SPINE_STRIP[1])
    d.rectangle((x0, y0, x0 + band, y1), fill=shade(colour, 0.7))
    # a stripe for spine art; the shelf writes the title over it
    d.rectangle((x0 + band // 2 - 6, y0, x0 + band // 2 + 6, y1), fill=shade(colour, 1.25))
    # a shape for art, then the title and the format
    cx, cy = (x0 + band + x1) // 2, (y0 + y1) // 2 - 40
    r = rng.randint(90, 150)
    shape = [(cx - r, cy - r), (cx + r, cy + r)]
    accent = shade(colour, 1.6) if sum(colour) < 420 else shade(colour, 0.55)
    (d.ellipse if rng.random() < 0.5 else d.rectangle)(shape, outline=accent, width=10)
    d.text((cx, y1 - 150), title, font=font(40), fill=ink(colour), anchor="mm")
    d.text((cx, y1 - 90), fmt, font=font(28), fill=ink(colour), anchor="mm")
    return jpeg(img)


def back(title: str, colour: tuple[int, int, int]) -> bytes:
    img = Image.new("RGB", FRAME, TABLE)
    d = ImageDraw.Draw(img)
    x0, y0, x1, y1 = case_box()
    d.rectangle((x0, y0, x1, y1), fill=shade(colour, 0.85))
    for i in range(6):
        y = y0 + 120 + i * 60
        d.line((x0 + 60, y, x1 - 60, y), fill=ink(colour), width=4)
    d.text(((x0 + x1) // 2, y0 + 60), title, font=font(34), fill=ink(colour), anchor="mm")
    return jpeg(img)


def build(root: Path, seed: int = 7) -> int:
    rng = random.Random(seed)
    db_path, photo_dir = root / "steelshelf.db", root / "photos"
    photo_dir.mkdir(parents=True)
    db.init_db(str(db_path))
    today = date.today()
    with db.connect(str(db_path)) as conn:
        for i, (title, fmt, edition, retailer, region, genre, director, colour, paid,
                ask) in enumerate(CASES):
            bought = today - timedelta(days=20 + i * 13)
            item_id = db.insert_item(conn, {
                "title": title, "format": fmt, "edition": edition, "retailer": retailer,
                "region": region, "condition": "sealed" if i % 3 else "opened",
                "genre": genre, "director": director, "paid_price": paid,
                "paid_on": bought.isoformat(),
            })
            db.set_film_by_hand(conn, item_id)  # no TMDB lookup on startup
            for kind, data in (("front", front(title, fmt, colour, rng)),
                               ("back", back(title, colour))):
                rel = photos.save(str(photo_dir), item_id, kind, ".jpg", data)
                db.insert_photo(conn, item_id, kind, rel)
            # a monthly re-price for each month since it was bought, drifting to `ask`
            months = max(1, (today - bought).days // 30)
            for m in range(months, -1, -1):
                drift = ask * (1 + rng.uniform(-0.12, 0.12)) * (1 - 0.03 * m)
                median = round(drift, 2)
                conn.execute(
                    "INSERT INTO valuations (item_id, source, low, median, high, n_listings,"
                    " fetched_at, via) VALUES (?, 'ebay_keyword', ?, ?, ?, ?, ?, ?)",
                    (item_id, round(median * 0.8, 2), median, round(median * 1.35, 2),
                     rng.randint(3, 14), f"{today - timedelta(days=30 * m)} 03:00:00",
                     "monthly" if m else None),
                )
        conn.commit()
    return len(CASES)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--dir", default="demo", help="where to write it (default ./demo)")
    ap.add_argument("--force", action="store_true", help="replace an existing one")
    args = ap.parse_args()
    root = Path(args.dir)
    if root.exists():
        if not args.force:
            sys.exit(f"{root} exists; --force replaces it")
        shutil.rmtree(root)
    n = build(root)
    print(f"{n} demo cases in {root}/. Run the app on it:")
    print(f"  DATABASE_PATH={root}/steelshelf.db PHOTO_DIR={root}/photos REPRICE_ENABLED=false \\")
    print("      .venv/bin/uvicorn app.main:app --port 8010")


if __name__ == "__main__":
    main()
