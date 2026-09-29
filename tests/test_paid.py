"""Paid price and date: typed on the forms, kept, and set against the worth."""

from app import db
from app import settings as settings_module

from tests.conftest import AUTH
from tests.test_items import StubSource, post_item, stub_pricing  # noqa: F401


def rows(sql, *args):
    with db.connect(settings_module.settings.database_path) as conn:
        return conn.execute(sql, args).fetchall()


def test_paid_price_and_date_are_saved_and_shown(admin):
    loc = post_item(admin, data={"paid_price": "$34.99", "paid_on": "2025-11-02"}).headers["location"]
    (item,) = rows("SELECT paid_price, paid_on FROM items")
    assert (item["paid_price"], item["paid_on"]) == (34.99, "2025-11-02")
    page = admin.get(loc).text
    assert "34.99" in page and "2025-11-02" in page
    edit = admin.get(f"{loc}/edit", auth=AUTH).text
    assert 'value="34.99"' in edit and 'value="2025-11-02"' in edit


def test_blank_paid_fields_stay_null_and_a_bad_price_is_refused(admin):
    post_item(admin)
    (item,) = rows("SELECT paid_price, paid_on FROM items")
    assert (item["paid_price"], item["paid_on"]) == (None, None)
    r = post_item(admin, data={"title": "Heat", "paid_price": "cheap"})
    assert r.status_code == 422 and "paid price must be a number" in r.text
    r = post_item(admin, data={"title": "Heat", "paid_price": "-5"})
    assert r.status_code == 422 and "cannot be negative" in r.text
    assert len(rows("SELECT * FROM items")) == 1


def test_edit_changes_the_paid_price(admin):
    loc = post_item(admin, data={"paid_price": "20"}).headers["location"]
    r = admin.post(loc, data={"title": "Alien", "paid_price": "25.50", "paid_on": "2026-01-05"},
                   auth=AUTH, follow_redirects=False)
    assert r.status_code == 303
    (item,) = rows("SELECT paid_price, paid_on FROM items")
    assert (item["paid_price"], item["paid_on"]) == (25.5, "2026-01-05")
    admin.post(loc, data={"title": "Alien", "paid_price": ""}, auth=AUTH)
    assert rows("SELECT paid_price FROM items")[0]["paid_price"] is None


def test_gain_on_the_item_page_and_the_shelf_total(admin, stub_pricing):  # noqa: F811
    stub_pricing(StubSource())  # median 35.00
    up = post_item(admin, data={"title": "Alien", "paid_price": "20"}).headers["location"]
    down = post_item(admin, data={"title": "Heat", "paid_price": "50"}).headers["location"]
    post_item(admin, data={"title": "Jaws", "paid_price": "10"})  # paid, never priced
    for loc in (up, down):
        admin.post(f"{loc}/value", auth=AUTH)

    page = admin.get(up).text
    assert "You paid" in page and 'class="small up">up 15.00 · 1.8×' in page
    page = admin.get(down).text
    assert 'class="small down">down 15.00 · 0.7×' in page

    shelf = admin.get("/").text
    assert "You paid 80 USD for\n      3 of them" in shelf
    assert 'class="up">up\n      0</span> on the 2 priced' in shelf


def test_a_paid_item_without_a_price_shows_paid_in_details(admin):
    loc = post_item(admin, data={"paid_price": "12", "paid_on": "2026-02-01"}).headers["location"]
    page = admin.get(loc).text
    assert "<dt>paid</dt><dd>12.00 on 2026-02-01</dd>" in page
