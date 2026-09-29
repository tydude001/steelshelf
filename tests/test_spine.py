"""The real spine photo on the shelf: finding the case, choosing it, cropping it."""

import io

import numpy as np
from PIL import Image, ImageDraw

from app import db, photos, shelf, spine
from app import settings as settings_module
from app.main import backfill_derived

from tests.conftest import AUTH

W, H = 900, 1200
CASE = (400, 120, 500, 1080)  # x0, y0, x1, y1: a 1:9.6 face, like a steelbook's


def spine_photo(printed=True, case=CASE, ground=(120, 95, 70)) -> bytes:
    """A case upright on rough carpet: brown noise, and a smooth face with a title."""
    rng = np.random.default_rng(7)
    noise = rng.integers(-40, 40, size=(H, W, 1))
    img = Image.fromarray(np.clip(np.array(ground) + noise, 0, 255).astype("uint8"))
    d = ImageDraw.Draw(img)
    d.rectangle(case, fill=(40, 42, 46))
    if printed:
        for y in range(case[1] + 200, case[3] - 200, 36):  # white letters down the spine
            d.rectangle((case[0] + 30, y, case[2] - 30, y + 22), fill=(245, 245, 240))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=92)
    return out.getvalue()


def front_photo() -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (600, 800), (180, 40, 40)).save(out, "JPEG")
    return out.getvalue()


def post_item(client, spine_bytes=None, title="Drive"):
    files = {"front": ("f.jpg", front_photo(), "image/jpeg")}
    if spine_bytes is not None:
        files["spine"] = ("s.jpg", spine_bytes, "image/jpeg")
    r = client.post("/items", data={"title": title}, files=files, auth=AUTH,
                    follow_redirects=False)
    assert r.status_code == 303
    return int(r.headers["location"].rsplit("/", 1)[1])


def item(item_id):
    with db.connect(settings_module.settings.database_path) as conn:
        return db.get_item(conn, item_id)


# --- finding the case ----------------------------------------------------------


def test_detect_finds_the_case_on_carpet():
    box = spine.detect(spine_photo())
    assert box is not None
    assert abs(box.x0 * W - CASE[0]) < 20 and abs(box.x1 * W - CASE[2]) < 20
    assert abs(box.y0 * H - CASE[1]) < 40 and abs(box.y1 * H - CASE[3]) < 40
    assert abs(box.angle) <= 1


def test_detect_finds_nothing_in_a_blank_frame():
    out = io.BytesIO()
    Image.new("RGB", (W, H), (120, 95, 70)).save(out, "JPEG")
    assert spine.detect(out.getvalue()) is None
    assert spine.detect(b"not a photo") is None


def test_a_quarter_turn_moves_the_case_sideways():
    box = spine.detect(spine_photo(), turns=1)
    # Turned on its side the upright case lies across the frame: not case-shaped.
    assert box is None or box.turns == 1


def test_cut_is_the_face_at_true_proportions():
    data = spine_photo()
    strip = spine.cut(data, spine.Box(CASE[0] / W, CASE[1] / H, CASE[2] / W, CASE[3] / H))
    assert strip.height == spine.STRIP_H
    assert abs(strip.width / strip.height - 100 / 960) < 0.01


def test_print_reads_and_bare_steel_does_not():
    box = spine.Box(CASE[0] / W, CASE[1] / H, CASE[2] / W, CASE[3] / H)
    assert spine.reads(spine.cut(spine_photo(), box))
    assert not spine.reads(spine.cut(spine_photo(printed=False), box))


def test_box_round_trips_and_bad_text_is_none():
    box = spine.Box(0.1, 0.2, 0.3, 0.9, -1.5, 1)
    assert spine.Box.loads(box.dumps()) == box
    assert spine.Box.loads("") is None and spine.Box.loads("{nope") is None


# --- which spine the shelf shows ---------------------------------------------------


def row(**kw):
    base = {"spine_box": None, "spine_ratio": None, "spine_reads": None, "spine_choice": None}
    return base | kw


def test_spine_face_is_the_photo_only_when_it_reads_or_is_chosen():
    cut = {"spine_box": "{}", "spine_ratio": 0.1}
    assert shelf.spine_face(row(**cut, spine_reads=1)) == ("photo", 0.1)
    assert shelf.spine_face(row(**cut, spine_reads=0))[0] == "edge"
    assert shelf.spine_face(row(**cut, spine_reads=0, spine_choice="photo"))[0] == "photo"
    assert shelf.spine_face(row(**cut, spine_reads=1, spine_choice="edge"))[0] == "edge"
    # Nothing cut: the front edge whatever was chosen.
    assert shelf.spine_face(row(spine_box="", spine_choice="photo")) == ("edge", shelf.EDGE_RATIO)


def test_spine_check_names_what_needs_a_look():
    assert shelf.spine_check(row()) is None  # no spine photo
    assert "No case" in shelf.spine_check(row(spine_box=""))
    assert "faint" in shelf.spine_check(row(spine_box="{}", spine_ratio=0.1, spine_reads=0))
    assert "slipcover" in shelf.spine_check(row(spine_box="{}", spine_ratio=0.2, spine_reads=1))
    assert shelf.spine_check(row(spine_box="{}", spine_ratio=0.1, spine_reads=1)) is None
    assert shelf.spine_check(row(spine_box="", spine_choice="edge")) is None  # decided


# --- through the app ---------------------------------------------------------------


def test_a_legible_spine_photo_goes_on_the_shelf(admin):
    item_id = post_item(admin, spine_photo())
    it = item(item_id)
    assert spine.Box.loads(it["spine_box"]) is not None and it["spine_reads"] == 1
    page = admin.get("/").text
    assert 'class="spine photo"' in page and f"/items/{item_id}/spine-photo.jpg?v=" in page
    r = admin.get(f"/items/{item_id}/spine-photo.jpg")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    assert "its spine photo" in admin.get(f"/items/{item_id}").text


def test_a_faint_spine_shows_the_front_edge_and_waits_on_review(admin):
    item_id = post_item(admin, spine_photo(printed=False))
    assert item(item_id)["spine_reads"] == 0
    assert 'class="spine photo"' not in admin.get("/").text
    review = admin.get("/review").text
    assert "Spines to check" in review and "Too faint to read" in review

    r = admin.post(f"/items/{item_id}/spine", auth=AUTH, follow_redirects=False,
                   data={"action": "choose", "choice": "photo", "next": "/review"})
    assert r.status_code == 303 and r.headers["location"] == "/review"
    assert item(item_id)["spine_choice"] == "photo"
    assert 'class="spine photo"' in admin.get("/").text
    assert "Too faint to read" not in admin.get("/review").text


def test_an_item_without_a_spine_photo_is_left_alone(admin):
    item_id = post_item(admin)
    assert item(item_id)["spine_box"] is None
    assert admin.get(f"/items/{item_id}/spine", auth=AUTH).status_code == 404
    assert admin.get(f"/items/{item_id}/spine-photo.jpg").status_code == 404
    assert "Change</a>" not in admin.get(f"/items/{item_id}").text


def test_the_crop_editor_saves_a_drawn_box(admin):
    item_id = post_item(admin, spine_photo())
    page = admin.get(f"/items/{item_id}/spine", auth=AUTH)
    assert page.status_code == 200 and 'id="crop-box"' in page.text
    assert admin.get(f"/items/{item_id}/spine").status_code == 401

    r = admin.post(f"/items/{item_id}/spine", auth=AUTH, follow_redirects=False, data={
        "action": "save", "choice": "photo", "x0": "40", "x1": "60", "y0": "10", "y1": "90",
        "angle": "0", "turns": "0"})
    assert r.status_code == 303
    it = item(item_id)
    box = spine.Box.loads(it["spine_box"])
    assert (box.x0, box.x1, box.y0, box.y1) == (0.4, 0.6, 0.1, 0.9)
    assert it["spine_choice"] == "photo"
    assert abs(it["spine_ratio"] - (0.2 * W) / (0.8 * H)) < 0.01

    bad = admin.post(f"/items/{item_id}/spine", auth=AUTH, data={
        "action": "save", "x0": "50", "x1": "50", "y0": "10", "y1": "90"})
    assert bad.status_code == 422


def test_rotate_and_find_again_rerun_detection(admin):
    item_id = post_item(admin, spine_photo())
    r = admin.post(f"/items/{item_id}/spine", auth=AUTH, data={"action": "rotate", "turns": "0"})
    assert r.status_code == 200 and 'name="turns" value="1"' in r.text
    frame = admin.get(f"/items/{item_id}/spine-frame.jpg?turns=1&angle=0")
    w, h = Image.open(io.BytesIO(frame.content)).size
    assert w > h  # the upright photo, turned on its side

    r = admin.post(f"/items/{item_id}/spine", auth=AUTH, data={"action": "redetect"})
    assert r.status_code == 200 and 'name="turns" value="0"' in r.text
    assert spine.Box.loads(item(item_id)["spine_box"]) is not None


def test_backfill_measures_spines_saved_before_this_and_recuts_on_request(admin):
    item_id = post_item(admin, spine_photo())
    with db.connect(settings_module.settings.database_path) as conn:
        conn.execute("UPDATE items SET spine_box = NULL, spine_ratio = NULL, spine_reads = NULL")
    strip = photos.spine_photo_path(settings_module.settings.photo_dir, item_id)
    strip.unlink()

    backfill_derived()
    assert item(item_id)["spine_box"] and strip.is_file()
    strip.unlink()
    assert admin.get(f"/items/{item_id}/spine-photo.jpg").status_code == 200
    assert strip.is_file()
