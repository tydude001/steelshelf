"""Spine colours: taken from the front photo at save, backfilled at startup."""

import colorsys
import io
import sqlite3

from PIL import Image

from app import db, photos
from app import settings as settings_module
from app.main import backfill_spine_colors

from tests.conftest import AUTH


def jpeg(color, size=(300, 400), inset=None) -> bytes:
    img = Image.new("RGB", size, color)
    if inset:  # a block of another colour in the middle of the frame
        w, h = size
        img.paste(inset, (w // 4, h // 4, 3 * w // 4, 3 * h // 4))
    out = io.BytesIO()
    img.save(out, "JPEG")
    return out.getvalue()


def hls(hex_color):
    return colorsys.rgb_to_hls(*(int(hex_color[i : i + 2], 16) / 255 for i in (1, 3, 5)))


def rows(sql, *args):
    with db.connect(settings_module.settings.database_path) as conn:
        return conn.execute(sql, args).fetchall()


# --- the colour itself -------------------------------------------------------


def test_a_red_cover_gives_a_red_spine():
    hue, _, sat = hls(photos.spine_color(jpeg((200, 30, 30))))
    assert hue < 0.03 or hue > 0.97
    assert sat > 0.5


def test_colour_wins_over_a_bigger_grey_area():
    # Mostly dark grey, with a teal block of a quarter of the frame: teal wins.
    color = photos.spine_color(jpeg((40, 40, 44), inset=(20, 150, 140)))
    hue, _, _ = hls(color)
    assert 0.45 < hue < 0.52


def test_lightness_is_clamped_to_read_as_metal():
    lo, hi = photos.SPINE_LIGHTNESS
    for c in ((255, 255, 255), (0, 0, 0), (255, 250, 200)):
        _, light, _ = hls(photos.spine_color(jpeg(c)))
        assert lo - 0.01 <= light <= hi + 0.01


def test_undecodable_photo_gives_none():
    assert photos.spine_color(b"\xff\xd8\xff\xe0not-a-jpeg") is None


def test_ink_is_dark_on_light_cases_and_light_on_dark():
    assert photos.spine_ink("#B8BEC5") == photos.DARK_INK
    assert photos.spine_ink("#2B3A5C") == photos.LIGHT_INK


# --- saving and showing it ---------------------------------------------------


def test_save_stores_the_colour_and_the_shelf_paints_it(admin):
    r = admin.post(
        "/items", data={"title": "Dune"}, auth=AUTH, follow_redirects=False,
        files={"front": ("f.jpg", jpeg((200, 30, 30)), "image/jpeg")},
    )
    assert r.status_code == 303
    (item,) = rows("SELECT spine_color FROM items")
    assert item["spine_color"].startswith("#")
    assert f'style="--tone: {item["spine_color"]}; --ink: ' in admin.get("/").text


def test_photo_with_no_colour_is_marked_tried_and_falls_back(admin):
    admin.post(
        "/items", data={"title": "Dune"}, auth=AUTH,
        files={"front": ("f.jpg", b"\xff\xd8\xff\xe0fake", "image/jpeg")},
    )
    (item,) = rows("SELECT spine_color FROM items")
    assert item["spine_color"] == ""  # not NULL: the backfill will not retry it
    assert "--tone" not in admin.get("/").text


def test_startup_backfills_items_saved_before_spine_colours(admin):
    admin.post(
        "/items", data={"title": "Dune"}, auth=AUTH,
        files={"front": ("f.jpg", jpeg((20, 150, 140)), "image/jpeg")},
    )
    with db.connect(settings_module.settings.database_path) as conn:
        conn.execute("UPDATE items SET spine_color = NULL")
        conn.commit()
    backfill_spine_colors()
    (item,) = rows("SELECT spine_color FROM items")
    assert item["spine_color"].startswith("#")


def test_init_db_adds_the_column_to_an_old_database(tmp_path):
    path = str(tmp_path / "old.db")
    # The items table as it shipped before spine_color.
    conn = sqlite3.connect(path)
    conn.execute(
        "CREATE TABLE items (id INTEGER PRIMARY KEY, title TEXT NOT NULL, format TEXT,"
        " edition TEXT, retailer TEXT, region TEXT, upc TEXT, condition TEXT, notes TEXT,"
        " created_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    conn.execute("INSERT INTO items (title) VALUES ('Alien')")
    conn.commit()
    conn.close()
    db.init_db(path)
    db.init_db(path)  # idempotent
    with db.connect(path) as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(items)")}
        assert "spine_color" in cols
        assert conn.execute("SELECT title FROM items").fetchone()["title"] == "Alien"
