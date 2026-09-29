"""Local barcode decode: off a photo, ahead of Claude's reading, and at save."""

import io

import zxingcpp
from PIL import Image

from app import barcode, db
from app import settings as settings_module
from app.identify import Identification

from tests.conftest import AUTH
from tests.test_identify import FOUND, StubIdentifier, jpeg, stub_identifier  # noqa: F401

UPC = "036000291452"


def barcode_photo(code=UPC, size=(1600, 1200), fmt=zxingcpp.BarcodeFormat.UPCA) -> bytes:
    """A grey 'photo' with the barcode printed small in its lower half."""
    bars = zxingcpp.create_barcode(code, fmt).to_image(scale=3)
    bars = Image.frombuffer("L", (bars.shape[1], bars.shape[0]), bars, "raw", "L", 0, 1)
    frame = Image.new("L", size, 140)
    frame.paste(bars, (size[0] // 2 - bars.width // 2, int(size[1] * 0.7)))
    out = io.BytesIO()
    frame.convert("RGB").save(out, "JPEG", quality=90)
    return out.getvalue()


def rows(sql, *args):
    with db.connect(settings_module.settings.database_path) as conn:
        return conn.execute(sql, args).fetchall()


# --- decode --------------------------------------------------------------------


def test_decode_reads_a_upc_a_as_its_twelve_printed_digits():
    assert barcode.decode(barcode_photo()) == UPC


def test_decode_reads_an_ean13():
    assert barcode.decode(barcode_photo("4006381333931", fmt=zxingcpp.BarcodeFormat.EAN13)) == (
        "4006381333931")


def test_decode_finds_nothing_in_a_plain_photo_or_junk():
    assert barcode.decode(jpeg(400, 300)) is None
    assert barcode.decode(b"\xff\xd8not a jpeg") is None


def test_decode_any_tries_the_back_first():
    other = barcode_photo("4006381333931", fmt=zxingcpp.BarcodeFormat.EAN13)
    assert barcode.decode_any([("front", jpeg()), ("other", other), ("back", barcode_photo())]) == UPC
    assert barcode.decode_any([("front", jpeg()), ("other", other)]) == "4006381333931"
    assert barcode.decode_any([("front", jpeg())]) is None


def test_apply_takes_the_decode_over_the_reading_and_notes_a_disagreement():
    read = Identification({**FOUND.values, "upc": "036000291453"}, ["retailer: peeled"])
    out = barcode.apply(read, UPC)
    assert out.values["upc"] == UPC
    assert out.doubts[0].startswith(f"upc: the barcode decodes to {UPC}; Claude read 036000291453")
    assert out.doubts[1] == "retailer: peeled"
    agreed = barcode.apply(FOUND, UPC)
    assert agreed.values["upc"] == UPC and agreed.doubts == FOUND.doubts
    blank = barcode.apply(Identification({**FOUND.values, "upc": None}), UPC)
    assert blank.values["upc"] == UPC and blank.doubts == []
    assert barcode.apply(FOUND, None) is FOUND


# --- the routes ------------------------------------------------------------------


def test_identify_fills_the_decoded_upc_over_claudes(admin, stub_identifier):  # noqa: F811
    stub_identifier(StubIdentifier(Identification({**FOUND.values, "upc": "036000291453"})))
    r = admin.post("/add/identify", auth=AUTH,
                   files={"front": ("f.jpg", jpeg(), "image/jpeg"),
                          "back": ("b.jpg", barcode_photo(), "image/jpeg")})
    assert r.status_code == 200
    assert f'name="upc" inputmode="numeric" pattern="[0-9]*"\n           value="{UPC}"' in r.text
    assert "the barcode decodes to" in r.text


def test_identify_failure_still_offers_the_decoded_upc(admin, stub_identifier):  # noqa: F811
    stub_identifier(StubIdentifier(error="down"))
    r = admin.post("/add/identify", auth=AUTH,
                   files={"front": ("f.jpg", jpeg(), "image/jpeg"),
                          "back": ("b.jpg", barcode_photo(), "image/jpeg")})
    assert r.status_code == 502 and f'value="{UPC}"' in r.text


def test_save_without_a_upc_reads_it_from_the_back_photo(admin):
    r = admin.post("/items", data={"title": "Alien"}, auth=AUTH, follow_redirects=False,
                   files={"front": ("f.jpg", jpeg(), "image/jpeg"),
                          "back": ("b.jpg", barcode_photo(), "image/jpeg")})
    assert r.status_code == 303
    assert rows("SELECT upc FROM items")[0]["upc"] == UPC
    # A typed UPC is kept as typed.
    admin.post("/items", data={"title": "Heat", "upc": "4006381333931"}, auth=AUTH,
               files={"front": ("f.jpg", jpeg(), "image/jpeg"),
                      "back": ("b.jpg", barcode_photo(), "image/jpeg")})
    assert rows("SELECT upc FROM items WHERE title = 'Heat'")[0]["upc"] == "4006381333931"


def test_reidentify_takes_the_decode_too(admin, stub_identifier):  # noqa: F811
    admin.post("/items", data={"title": "Alien"}, auth=AUTH,
               files={"front": ("f.jpg", jpeg(), "image/jpeg")})
    (item,) = rows("SELECT id FROM items")
    # Put a barcode photo on the saved item, then re-identify with a model that misreads.
    admin_photos = settings_module.settings.photo_dir
    with db.connect(settings_module.settings.database_path) as conn:
        from app import photos as photomod
        rel = photomod.save(admin_photos, item["id"], "back", ".jpg", barcode_photo())
        db.insert_photo(conn, item["id"], "back", rel)
        conn.commit()
    stub_identifier(StubIdentifier(Identification({**FOUND.values, "upc": "036000291453"})))
    r = admin.post(f"/items/{item['id']}/identify", auth=AUTH)
    assert r.status_code == 200
    assert f"upc: — → {UPC}" in r.text
