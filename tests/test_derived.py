"""Derived images: a thumbnail per photo and an art strip per item's spine."""

import io

from PIL import Image

from app import db, photos
from app import settings as settings_module
from app.main import backfill_derived

from tests.conftest import AUTH


def jpeg(size=(1200, 1600), left=(200, 30, 30), right=(30, 30, 200)) -> bytes:
    """A tall photo whose left half is one colour and right half another."""
    img = Image.new("RGB", size, right)
    img.paste(left, (0, 0, size[0] // 2, size[1]))
    out = io.BytesIO()
    img.save(out, "JPEG")
    return out.getvalue()


def post_item(client, front=None, title="Alien"):
    files = {"front": ("f.jpg", front or jpeg(), "image/jpeg")}
    return client.post("/items", data={"title": title}, files=files, auth=AUTH,
                       follow_redirects=False)


def rows(sql, *args):
    with db.connect(settings_module.settings.database_path) as conn:
        return conn.execute(sql, args).fetchall()


# --- the images themselves ---------------------------------------------------


def test_thumbnail_is_bounded_and_upright():
    thumb = Image.open(io.BytesIO(photos.thumbnail(jpeg())))
    assert max(thumb.size) == photos.THUMB_EDGE
    assert thumb.size[1] > thumb.size[0]  # tall stays tall


def test_spine_strip_is_the_covers_left_edge_at_true_proportions():
    strip = Image.open(io.BytesIO(photos.spine_strip(jpeg())))
    assert strip.size == photos.SPINE_STRIP
    # Cut from the left of the case, so the left half's colour, not the right's.
    r, g, b = strip.getpixel((strip.size[0] // 2, strip.size[1] // 2))
    assert r > 120 and b < 80


def test_undecodable_photo_gives_no_derived_image():
    assert photos.thumbnail(b"\xff\xd8not a jpeg") is None
    assert photos.spine_strip(b"\xff\xd8not a jpeg") is None


# --- made at save, served by route ----------------------------------------------


def test_save_writes_thumb_and_spine_files(admin):
    loc = post_item(admin).headers["location"]
    item_id = int(loc.rsplit("/", 1)[1])
    (p,) = rows("SELECT id FROM photos")
    assert photos.thumb_path(settings_module.settings.photo_dir, p["id"]).is_file()
    assert photos.spine_path(settings_module.settings.photo_dir, item_id).is_file()

    r = admin.get(f"/photos/{p['id']}/thumb")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert max(Image.open(io.BytesIO(r.content)).size) == photos.THUMB_EDGE
    r = admin.get(f"{loc}/spine.jpg")
    assert r.status_code == 200
    assert Image.open(io.BytesIO(r.content)).size == photos.SPINE_STRIP


def test_shelf_paints_the_spine_art(admin):
    loc = post_item(admin).headers["location"]
    shelf = admin.get("/").text
    assert 'class="spine c' in shelf and " art" in shelf.split('class="spine c')[1][:20]
    assert f"--art: url({loc}/spine.jpg)" in shelf


def test_undecodable_photo_serves_the_original_and_no_spine(admin):
    fake = b"\xff\xd8\xff\xe0fake-jpeg-bytes"
    loc = post_item(admin, front=fake).headers["location"]
    (p,) = rows("SELECT id FROM photos")
    r = admin.get(f"/photos/{p['id']}/thumb")
    assert r.status_code == 200 and r.content == fake
    assert admin.get(f"{loc}/spine.jpg").status_code == 404
    assert admin.get("/items/999/spine.jpg").status_code == 404
    assert admin.get("/photos/999/thumb").status_code == 404


def test_missing_derived_images_are_made_on_request_and_by_backfill(admin):
    loc = post_item(admin).headers["location"]
    item_id = int(loc.rsplit("/", 1)[1])
    (p,) = rows("SELECT id FROM photos")
    thumb = photos.thumb_path(settings_module.settings.photo_dir, p["id"])
    spine = photos.spine_path(settings_module.settings.photo_dir, item_id)
    thumb.unlink()
    spine.unlink()

    assert admin.get(f"/photos/{p['id']}/thumb").status_code == 200
    assert thumb.is_file()
    thumb.unlink()
    assert admin.get(f"{loc}/spine.jpg").status_code == 200
    assert spine.is_file()
    spine.unlink()

    backfill_derived()  # what startup runs, for items saved before this existed
    assert thumb.is_file() and spine.is_file()
