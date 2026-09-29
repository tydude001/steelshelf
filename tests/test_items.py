"""Photo form, item save, item page, photo serving, and the valuation POST."""

from pathlib import Path

import pytest

from app import db
from app import settings as settings_module
from app.main import app, get_pricing, get_sold_pricing
from app.pricing import Listing, PricingError, Quote, from_listings

from tests.conftest import AUTH

JPEG = b"\xff\xd8\xff\xe0fake-jpeg-bytes"


def photo(name="p.jpg", data=JPEG, ctype="image/jpeg"):
    return (name, data, ctype)


def post_item(client, data=None, files=None, auth=AUTH):
    data = {"title": "Alien", "upc": "012345678905", **(data or {})}
    files = files if files is not None else {"front": photo()}
    return client.post("/items", data=data, files=files, auth=auth, follow_redirects=False)


def rows(sql, *args):
    with db.connect(settings_module.settings.database_path) as conn:
        return conn.execute(sql, args).fetchall()


# --- auth -----------------------------------------------------------------


def test_writes_fail_closed_without_admin_config(client):
    assert client.get("/add").status_code == 503
    assert post_item(client).status_code == 503
    assert rows("SELECT * FROM items") == []


def test_writes_need_credentials(admin):
    r = admin.get("/add")
    assert r.status_code == 401
    assert r.headers["www-authenticate"].startswith("Basic")
    assert post_item(admin, auth=("admin", "wrong")).status_code == 401
    assert rows("SELECT * FROM items") == []


def test_reads_stay_open(admin):
    assert admin.get("/").status_code == 200


# --- the form ---------------------------------------------------------------


def test_add_form_asks_the_camera_for_three_photos(admin):
    r = admin.get("/add", auth=AUTH)
    assert r.status_code == 200
    for kind in ("front", "spine", "back"):
        assert f'name="{kind}" type="file" accept="image/*" capture="environment"' in r.text
    assert 'enctype="multipart/form-data"' in r.text


def test_save_writes_item_photos_and_files(admin):
    files = {"front": photo(), "spine": photo(ctype="image/png"), "back": photo()}
    r = post_item(admin, data={"retailer": "Zavvi", "notes": "  "}, files=files)
    assert r.status_code == 303
    item_id = int(r.headers["location"].rsplit("/", 1)[1])

    (item,) = rows("SELECT * FROM items")
    assert (item["id"], item["title"], item["retailer"]) == (item_id, "Alien", "Zavvi")
    assert item["notes"] is None  # whitespace-only is stored as NULL
    assert item["edition"] is None  # blank field, not the form's default text

    stored = rows("SELECT kind, path FROM photos WHERE item_id = ? ORDER BY id", item_id)
    assert [p["kind"] for p in stored] == ["front", "spine", "back"]
    assert stored[1]["path"].startswith(f"{item_id}/spine-")
    assert stored[1]["path"].endswith(".png")
    root = Path(settings_module.settings.photo_dir)
    assert all((root / p["path"]).read_bytes() == JPEG for p in stored)


def test_front_only_is_enough(admin):
    assert post_item(admin).status_code == 303
    assert len(rows("SELECT * FROM photos")) == 1


def test_missing_title_and_front_rerenders_with_values(admin):
    r = post_item(admin, data={"title": " ", "retailer": "Best Buy"}, files={})
    assert r.status_code == 422
    assert "title is required" in r.text
    assert "a front photo is required" in r.text
    assert 'value="Best Buy"' in r.text
    assert rows("SELECT * FROM items") == []


def test_non_image_upload_rejected_and_nothing_written(admin):
    files = {"front": photo(), "back": photo("x.pdf", b"%PDF", "application/pdf")}
    r = post_item(admin, files=files)
    assert r.status_code == 422
    assert "back: application/pdf is not a supported image" in r.text
    assert rows("SELECT * FROM items") == []
    assert list(Path(settings_module.settings.photo_dir).iterdir()) == []


def test_non_digit_upc_rejected(admin):
    r = post_item(admin, data={"upc": "0123-4567"})
    assert r.status_code == 422
    assert "UPC is digits only" in r.text


def test_failed_photo_insert_rolls_back_item_and_files(admin, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(db, "insert_photo", boom)
    with pytest.raises(RuntimeError):
        post_item(admin)
    assert rows("SELECT * FROM items") == []
    assert list(Path(settings_module.settings.photo_dir).iterdir()) == []


# --- reading it back --------------------------------------------------------


def test_item_page_and_library_show_the_item(admin):
    loc = post_item(admin, data={"retailer": "Zavvi"}).headers["location"]
    page = admin.get(loc)
    assert page.status_code == 200
    assert "Alien" in page.text and "Zavvi" in page.text
    (p,) = rows("SELECT id FROM photos")
    # Pages show the thumbnail and link to the original.
    assert f'href="/photos/{p["id"]}"' in page.text
    assert f'src="/photos/{p["id"]}/thumb"' in page.text

    library = admin.get("/")
    assert f'href="{loc}"' in library.text
    assert f'src="/photos/{p["id"]}/thumb"' in library.text


def test_photo_served_by_row_id(admin):
    post_item(admin)
    (p,) = rows("SELECT id FROM photos")
    r = admin.get(f"/photos/{p['id']}")
    assert r.status_code == 200
    assert r.content == JPEG
    assert r.headers["content-type"] == "image/jpeg"


def test_photo_row_pointing_outside_photo_dir_is_404(admin):
    post_item(admin)
    with db.connect(settings_module.settings.database_path) as conn:
        conn.execute("UPDATE photos SET path = '../t.db'")
        conn.commit()
    (p,) = rows("SELECT id FROM photos")
    assert admin.get(f"/photos/{p['id']}").status_code == 404


def test_unknown_item_and_photo_are_404(admin):
    assert admin.get("/items/999").status_code == 404
    assert admin.get("/photos/999").status_code == 404


# --- valuation POST ---------------------------------------------------------


class StubSource:
    name = "ebay_active"

    def __init__(self, error=None):
        self.error = error

    def quote(self, item):
        if self.error:
            raise PricingError(self.error)
        return Quote("ebay_active", 20.0, 35.0, 80.0, 7, "USD")


@pytest.fixture
def stub_pricing():
    def use(source):
        app.dependency_overrides[get_pricing] = lambda: source

    yield use
    app.dependency_overrides.pop(get_pricing, None)


def test_value_appends_and_shows_on_page(admin, stub_pricing):
    stub_pricing(StubSource())
    loc = post_item(admin).headers["location"]
    for _ in range(2):
        r = admin.post(f"{loc}/value", auth=AUTH, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == loc
    assert len(rows("SELECT * FROM valuations")) == 2
    assert "35.00" in admin.get(loc).text
    assert "ask ~35.00 USD" in admin.get("/").text


def test_value_error_is_shown_not_raised(admin, stub_pricing):
    stub_pricing(StubSource(error="eBay keyset missing"))
    loc = post_item(admin).headers["location"]
    r = admin.post(f"{loc}/value", auth=AUTH)
    assert r.status_code == 200
    assert "eBay keyset missing" in r.text
    assert rows("SELECT * FROM valuations") == []


def test_value_needs_admin(admin, stub_pricing):
    stub_pricing(StubSource())
    loc = post_item(admin).headers["location"]
    assert admin.post(f"{loc}/value").status_code == 401


def test_library_shelves_a_spine_per_item_and_sums_the_floor(admin, stub_pricing):
    stub_pricing(StubSource())
    locs = [post_item(admin, data={"title": t, "format": "4K UHD"}).headers["location"]
            for t in ("Alien", "Heat")]
    for loc in locs:
        admin.post(f"{loc}/value", auth=AUTH)
    post_item(admin, data={"title": "Jaws"})  # never valued: on the shelf, not in the sum
    library = admin.get("/").text
    shelf = library.split('class="shelves"')[1].split("</ul>")[0]
    assert shelf.count('class="spine ') == 3
    for loc in locs:
        assert f'href="{loc}" title=' in shelf
    assert ">4K<" in shelf
    assert "70 USD" in library  # two latest medians of 35.00


def test_sold_prices_append_and_label_the_page_and_library(admin, stub_pricing):
    stub_pricing(StubSource())
    loc = post_item(admin).headers["location"]
    admin.post(f"{loc}/value", auth=AUTH)
    r = admin.post(f"{loc}/sold", data={"prices": "$40, 50, 60"}, auth=AUTH,
                   follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == loc
    got = rows("SELECT source, median, n_listings FROM valuations ORDER BY id")
    assert [tuple(g) for g in got] == [("ebay_active", 35.0, 7), ("manual_sold", 50.0, 3)]
    page = admin.get(loc).text
    assert "Sold (median)" in page and "3 sales" in page
    library = admin.get("/").text
    assert "sold ~50.00 USD" in library
    assert "50 USD" in library


def test_sold_prices_typo_is_shown_and_nothing_written(admin):
    loc = post_item(admin).headers["location"]
    r = admin.post(f"{loc}/sold", data={"prices": "40, fourty"}, auth=AUTH)
    assert r.status_code == 200
    assert "not a price" in r.text
    assert rows("SELECT * FROM valuations") == []


def test_sold_prices_need_admin_and_an_item(admin):
    loc = post_item(admin).headers["location"]
    assert admin.post(f"{loc}/sold", data={"prices": "40"}).status_code == 401
    assert admin.post("/items/999/sold", data={"prices": "40"}, auth=AUTH).status_code == 404


def test_sold_lookup_is_off_unless_enabled(admin, monkeypatch):
    app.dependency_overrides[get_sold_pricing] = lambda: StubSold()
    try:
        loc = post_item(admin).headers["location"]
        assert "Look up eBay sold prices" not in admin.get(loc).text
        assert admin.post(f"{loc}/sold/lookup", auth=AUTH).status_code == 404
        monkeypatch.setattr(settings_module.settings, "sold_lookup_enabled", True)
        assert "Look up eBay sold prices" in admin.get(loc).text
        assert admin.post(f"{loc}/sold/lookup").status_code == 401
        r = admin.post(f"{loc}/sold/lookup", auth=AUTH, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == loc
        got = rows("SELECT source, median FROM valuations")
        assert [tuple(g) for g in got] == [("serpapi_sold", 42.0)]
        page = admin.get(loc).text
        assert "Sold (median)" in page and "fetched" in page
    finally:
        app.dependency_overrides.pop(get_sold_pricing, None)


class StubSold:
    name = "serpapi_sold"

    def quote(self, item):
        return Quote("serpapi_sold", 30.0, 42.0, 55.0, 5, "USD")


# --- editing ----------------------------------------------------------------


def saved_item(admin, **data):
    r = post_item(admin, data={"condition": "sealed", "retailer": "Best Buy", **data})
    return int(r.headers["location"].rsplit("/", 1)[1])


def test_edit_needs_credentials(admin):
    item_id = saved_item(admin)
    assert admin.get(f"/items/{item_id}/edit").status_code == 401
    r = admin.post(f"/items/{item_id}", data={"title": "X"}, auth=("admin", "wrong"))
    assert r.status_code == 401
    assert rows("SELECT title FROM items")[0]["title"] == "Alien"


def test_edit_form_is_prefilled_and_linked(admin):
    item_id = saved_item(admin)
    assert f'href="/items/{item_id}/edit"' in admin.get(f"/items/{item_id}").text
    r = admin.get(f"/items/{item_id}/edit", auth=AUTH)
    assert r.status_code == 200
    assert 'value="Alien"' in r.text
    assert '<option value="sealed" selected>' in r.text
    assert 'value="Steelbook"' not in r.text  # a blank edition stays blank, no add-form default


def test_edit_saves_fields_and_keeps_photos(admin):
    item_id = saved_item(admin)
    data = {"title": "Alien", "upc": "012345678905", "condition": "opened", "retailer": " "}
    r = admin.post(f"/items/{item_id}", data=data, auth=AUTH, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/items/{item_id}"
    (item,) = rows("SELECT * FROM items")
    assert (item["condition"], item["retailer"]) == ("opened", None)
    assert len(rows("SELECT * FROM photos WHERE item_id = ?", item_id)) == 1


def test_edit_rejects_bad_fields(admin):
    item_id = saved_item(admin)
    r = admin.post(f"/items/{item_id}", data={"title": " ", "upc": "12a"}, auth=AUTH)
    assert r.status_code == 422
    assert "title is required" in r.text and "UPC is digits only" in r.text
    assert rows("SELECT title FROM items")[0]["title"] == "Alien"


def test_edit_missing_item_is_404(admin):
    assert admin.get("/items/99/edit", auth=AUTH).status_code == 404
    assert admin.post("/items/99", data={"title": "X"}, auth=AUTH).status_code == 404


def test_hand_entered_prices_are_marked_not_live(admin, stub_pricing):
    stub_pricing(StubSource())
    typed, fetched = (post_item(admin, data={"title": t}).headers["location"]
                      for t in ("Alien", "Heat"))
    post_item(admin, data={"title": "Jaws"})  # never priced
    admin.post(f"{typed}/sold", data={"prices": "40, 50, 60"}, auth=AUTH)
    admin.post(f"{fetched}/value", auth=AUTH)
    assert "Entered by hand, not live." in admin.get(typed).text
    assert "Entered by hand" not in admin.get(fetched).text
    library = admin.get("/").text
    assert "sold ~50.00 USD · by hand" in library
    assert "ask ~35.00 USD<" in library
    assert "1 fetched from eBay · 1\n      entered by hand · 1 not priced" in library


@pytest.mark.parametrize("ebay_id, enabled, key, via_serpapi", [
    ("", True, "k", True),        # no eBay keyset, SerpApi on: asks through SerpApi
    ("id", True, "k", False),     # an eBay keyset always wins
    ("", False, "k", False),      # SerpApi off: the eBay API, which says what is missing
    ("", True, "", False),
])
def test_asks_via_serpapi_only_without_a_keyset(monkeypatch, ebay_id, enabled, key, via_serpapi):
    from app import main
    monkeypatch.setattr(main.settings, "ebay_client_id", ebay_id)
    monkeypatch.setattr(main.settings, "ebay_client_secret", ebay_id)
    monkeypatch.setattr(main.settings, "sold_lookup_enabled", enabled)
    monkeypatch.setattr(main.settings, "serpapi_key", key)
    monkeypatch.setattr(main, "_pricing", None)
    assert main.asks_via_serpapi() is via_serpapi
    assert main.get_pricing().name == ("serpapi_active" if via_serpapi else "ebay_keyword")


def test_opened_item_says_whose_copies_priced_it(admin, stub_pricing):
    stub_pricing(StubSource())
    opened = post_item(admin, data={"title": "Alien", "condition": "opened"}).headers["location"]
    sealed = post_item(admin, data={"title": "Heat", "condition": "sealed"}).headers["location"]
    for loc in (opened, sealed):
        admin.post(f"{loc}/value", auth=AUTH)
    assert "Price of sealed copies" in admin.get(opened).text
    assert "Price of sealed copies" not in admin.get(sealed).text


@pytest.mark.parametrize("soldcomps_key, name", [
    ("sc_key", "soldcomps_sold"),   # SoldComps wins when it has a key
    ("", "serpapi_sold"),
])
def test_sold_lookups_go_to_soldcomps_when_it_has_a_key(monkeypatch, soldcomps_key, name):
    from app import main
    monkeypatch.setattr(main.settings, "soldcomps_key", soldcomps_key)
    monkeypatch.setattr(main, "_sold_pricing", None)
    assert main.get_sold_pricing().name == name


class StubSoldComps:
    name = "soldcomps_sold"

    def quote(self, item):
        return Quote("soldcomps_sold", 30.0, 42.0, 55.0, 5, "USD")


def test_a_soldcomps_price_is_labelled_sold(admin, monkeypatch):
    app.dependency_overrides[get_sold_pricing] = lambda: StubSoldComps()
    monkeypatch.setattr(settings_module.settings, "sold_lookup_enabled", True)
    try:
        loc = post_item(admin).headers["location"]
        admin.post(f"{loc}/sold/lookup", auth=AUTH)
        assert "Sold (median)" in admin.get(loc).text
        assert "sold ~42.00 USD" in admin.get("/").text
    finally:
        app.dependency_overrides.pop(get_sold_pricing, None)


def test_price_buttons_say_they_are_working(admin, monkeypatch):
    monkeypatch.setattr(settings_module.settings, "sold_lookup_enabled", True)
    page = admin.get(post_item(admin).headers["location"]).text
    assert 'data-wait="Fetching prices…"' in page
    assert 'data-wait="Searching eBay sold listings…"' in page


class StubSoldOf:
    """A SoldComps quote whose sales were in these conditions."""

    name = "soldcomps_sold"

    def __init__(self, *conditions):
        self.conditions = conditions

    def quote(self, item):
        sales = [Listing(f"Alien Steelbook {i}", 40.0 + i, str(i), c)
                 for i, c in enumerate(self.conditions)]
        return from_listings("soldcomps_sold", sales, "USD")


@pytest.mark.parametrize("condition, sales, says", [
    ("opened", ("Pre-Owned", "Used"), "Sold prices of opened copies,"),
    ("sealed", ("Brand New",), "Sold prices of sealed copies,"),
    ("opened", ("Brand New", "Pre-Owned"), "Includes sealed copies"),
    ("sealed", ("Pre-Owned",), "Includes opened copies"),
])
def test_sold_price_says_whose_copies_sold(admin, monkeypatch, condition, sales, says):
    app.dependency_overrides[get_sold_pricing] = lambda: StubSoldOf(*sales)
    monkeypatch.setattr(settings_module.settings, "sold_lookup_enabled", True)
    try:
        loc = post_item(admin, data={"title": "Alien", "condition": condition}).headers["location"]
        admin.post(f"{loc}/sold/lookup", auth=AUTH)
        assert says in admin.get(loc).text
    finally:
        app.dependency_overrides.pop(get_sold_pricing, None)
