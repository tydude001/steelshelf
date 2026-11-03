"""Adding photos to a saved case: the step after import, whose cases have none."""

from pathlib import Path

from app import db, main
from app import settings as settings_module
from tests.conftest import AUTH
from tests.test_import import do_import, sheet
from tests.test_items import JPEG, photo, post_item, rows


def imported(admin, line="Alien,,,,,") -> int:
    do_import(admin, sheet(line))
    return rows("SELECT id FROM items")[-1]["id"]


def add(admin, item_id, files, auth=AUTH):
    return admin.post(f"/items/{item_id}/photos", files=files, auth=auth,
                      follow_redirects=False)


def test_an_imported_case_offers_every_kind_of_photo(admin):
    item_id = imported(admin)
    page = admin.get(f"/items/{item_id}").text
    assert '<details class="add-photos" id="add-photos" open>' in page
    for kind in ("front", "spine", "back", "other"):
        assert f'name="{kind}" type="file"' in page


def test_adding_a_front_saves_it_and_lands_on_identify_again(admin):
    item_id = imported(admin)
    r = add(admin, item_id, {"front": photo(), "back": photo()})
    assert r.status_code == 303
    assert r.headers["location"].startswith(f"/items/{item_id}/edit?added=front%2Cback")
    stored = rows("SELECT kind, path FROM photos WHERE item_id = ?", item_id)
    assert sorted(p["kind"] for p in stored) == ["back", "front"]
    root = Path(settings_module.settings.photo_dir)
    assert all((root / p["path"]).read_bytes() == JPEG for p in stored)
    edit = admin.get(r.headers["location"], auth=AUTH).text
    assert "Added: front, back photos" in edit
    assert 'action="/items/%d/identify"' % item_id in edit


def test_the_form_offers_only_the_kinds_still_missing(admin):
    post_item(admin, files={"front": photo(), "spine": photo()})
    page = admin.get("/items/1").text
    assert 'name="front" type="file"' not in page and 'name="spine" type="file"' not in page
    assert 'name="back" type="file"' in page and "Add a back, other photo" in page


def test_a_kind_the_case_has_is_never_replaced(admin):
    post_item(admin)
    (before,) = rows("SELECT * FROM photos")
    r = add(admin, 1, {"front": photo(data=b"\xff\xd8\xff\xe0other")})
    assert r.status_code == 303 and "photo_error=" in r.headers["location"]
    page = admin.get(r.headers["location"]).text
    assert "front: this case already has one" in page
    assert [tuple(p) for p in rows("SELECT * FROM photos")] == [tuple(before)]


def test_nothing_chosen_or_a_bad_file_is_refused_with_a_reason(admin):
    item_id = imported(admin)
    r = add(admin, item_id, {})
    assert "choose+a+photo" in r.headers["location"]
    r = add(admin, item_id, {"front": photo("x.txt", b"hi", "text/plain")})
    assert "photo_error=" in r.headers["location"]
    assert rows("SELECT * FROM photos") == []


def test_adding_photos_needs_the_admin_login(admin):
    item_id = imported(admin)
    assert add(admin, item_id, {"front": photo()}, auth=None).status_code == 401
    assert add(admin, 999, {"front": photo()}).status_code == 404
    assert rows("SELECT * FROM photos") == []


def test_a_barcode_on_the_photos_fills_a_blank_upc_only(admin, monkeypatch):
    monkeypatch.setattr(main.barcode, "decode_any", lambda shots: "012345678905")
    item_id = imported(admin)
    r = add(admin, item_id, {"back": photo()})
    assert rows("SELECT upc FROM items WHERE id = ?", item_id)[0]["upc"] == "012345678905"
    assert "the UPC 012345678905 read off them" in admin.get(r.headers["location"],
                                                              auth=AUTH).text
    other = imported(admin, "Heat,,,4006381333931,,")
    add(admin, other, {"back": photo()})
    assert rows("SELECT upc FROM items WHERE id = ?", other)[0]["upc"] == "4006381333931"


def test_identify_again_without_a_front_points_at_adding_one(admin):
    item_id = imported(admin)
    add(admin, item_id, {"back": photo()})
    edit = admin.get(f"/items/{item_id}/edit", auth=AUTH).text
    assert f'href="/items/{item_id}#add-photos"' in edit
    assert f'action="/items/{item_id}/identify"' not in edit


def test_a_failed_save_leaves_no_file_behind(admin, monkeypatch):
    item_id = imported(admin)

    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(db, "insert_photo", boom)
    try:
        add(admin, item_id, {"front": photo()})
    except RuntimeError:
        pass
    assert not list((Path(settings_module.settings.photo_dir) / str(item_id)).glob("*"))
