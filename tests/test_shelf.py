"""Shelf order: the sort keys, the grouped shelves and their plaques, the cookie."""

import sqlite3

from app import db, shelf
from app import settings as settings_module


from tests.conftest import AUTH
from tests.test_items import StubSource, post_item, stub_pricing  # noqa: F401


def row(**kw) -> sqlite3.Row:
    """A row shaped like db.list_items returns, with the columns shelve reads."""
    base = {"id": 0, "title": "", "retailer": None, "format": None, "region": None,
            "edition": None, "notes": None, "genre": None, "director": None,
            "created_at": "2026-09-25 00:00:00", "latest_median": None,
            "latest_currency": "USD"}
    base.update(kw)
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    cols = ", ".join(f"? AS {k}" for k in base)
    return conn.execute(f"SELECT {cols}", list(base.values())).fetchone()


ITEMS = [
    row(id=1, title="The Batman", retailer="Best Buy", format="4K + Blu-ray", region="US",
        created_at="2026-09-01", latest_median=99.0),
    row(id=2, title="Alien", retailer="Zavvi", format="Blu-ray", region="UK",
        created_at="2026-09-03", latest_median=40.0),
    row(id=3, title="Heat", retailer="Best Buy", format="4K UHD", region="US",
        created_at="2026-09-02"),
    row(id=4, title="An Elephant", format="DVD", created_at="2026-09-04", latest_median=5.0),
]


def titles(shelves):
    return [[it["title"] for it in s.items] for s in shelves]


def test_newest_is_the_default_and_one_shelf():
    (s,) = shelf.shelve(ITEMS, "newest")
    assert s.name is None
    assert [it["id"] for it in s.items] == [4, 2, 3, 1]
    assert titles(shelf.shelve(ITEMS, "bogus")) == titles(shelf.shelve(ITEMS, "newest"))


def test_title_ignores_a_leading_article():
    assert titles(shelf.shelve(ITEMS, "title")) == [["Alien", "The Batman", "An Elephant", "Heat"]]


def test_value_puts_the_priced_first_highest_down_and_the_unpriced_last():
    assert titles(shelf.shelve(ITEMS, "value")) == [["The Batman", "Alien", "An Elephant", "Heat"]]


def test_label_groups_by_retailer_largest_first_and_the_unnamed_last():
    shelves = shelf.shelve(ITEMS, "label")
    assert [s.name for s in shelves] == ["Best Buy", "Zavvi", shelf.NO_LABEL]
    assert titles(shelves)[0] == ["The Batman", "Heat"]  # by title within a group
    assert shelves[0].plaque == "Best Buy · 2 · 99 USD"  # Heat is unpriced
    assert shelves[2].plaque == f"{shelf.NO_LABEL} · 1 · 5 USD"


def test_format_groups_4k_first_then_blu_ray_then_dvd():
    assert [s.name for s in shelf.shelve(ITEMS, "format")] == [
        "4K + Blu-ray", "4K UHD", "Blu-ray", "DVD"]


def test_region_groups_and_a_group_with_nothing_priced_has_no_total():
    shelves = shelf.shelve(ITEMS, "region")
    assert [s.name for s in shelves] == ["US", "UK", shelf.NO_REGION]
    assert shelf.Shelf("X", [ITEMS[2]]).plaque == "X · 1"


# --- the route --------------------------------------------------------------------


def test_sort_param_orders_the_shelf_and_is_remembered(admin):
    for title, retailer in (("Alien", "Zavvi"), ("Heat", "Best Buy"), ("Jaws", "Best Buy")):
        post_item(admin, data={"title": title, "retailer": retailer})
    page = admin.get("/?sort=label")
    assert page.status_code == 200
    body = page.text
    assert body.index("Best Buy · 2") < body.index("Zavvi · 1")
    assert 'class="shelves group"' in body
    assert 'href="/?sort=label" class="on"' in body
    assert page.cookies.get("sort") == "label"

    # The next plain visit keeps the choice, and so does a bogus key; with no
    # cookie at all, newest.
    assert 'href="/?sort=label" class="on"' in admin.get("/").text
    assert 'href="/?sort=label" class="on"' in admin.get("/?sort=bogus").text
    admin.cookies.clear()
    assert 'href="/?sort=newest" class="on"' in admin.get("/").text
    body = admin.get("/?sort=title").text
    shelf_html = body.split('class="shelves"')[1].split("</ul>")[0]
    assert shelf_html.index("Alien") < shelf_html.index("Heat") < shelf_html.index("Jaws")


# --- the summary ----------------------------------------------------------------


def test_summary_is_a_value_line_and_tiles(admin, stub_pricing):  # noqa: F811
    stub_pricing(StubSource())  # median 35.00
    for title, retailer in (("Alien", "Zavvi"), ("Heat", "Best Buy"), ("Jaws", "Best Buy")):
        loc = post_item(admin, data={"title": title, "retailer": retailer}).headers["location"]
        admin.post(f"{loc}/value", auth=AUTH)
    body = admin.get("/").text
    assert '<span class="display">105 USD</span>' in body
    assert "3 steelbooks · asks" in body
    assert "Shelf value · 3 steelbooks" in body
    assert "Best Buy · 2" in body and "70 USD" in body and "67% of the shelf" in body
    assert "Needs a look" in body
    assert "You paid" not in body  # no paid price anywhere: no paid tile

    post_item(admin, data={"title": "Dune", "paid_price": "20"})
    body = admin.get("/").text
    assert '<div class="tile gain">' in body and "none of them priced yet" in body


def test_basis_names_what_the_total_is_made_of():
    from app.main import _basis
    ask = row(latest_median=10.0, latest_source="ebay_keyword")
    sold = row(latest_median=10.0, latest_source="manual_sold")
    unpriced = row(latest_source=None)
    assert _basis([ask, unpriced]) == "asks"
    assert _basis([sold]) == "sold prices"
    assert _basis([ask, sold]) == "asks and sold prices"
    assert _basis([unpriced]) == "no prices yet"


def test_sort_chips_fade_at_the_right_edge_on_a_phone():
    from pathlib import Path
    css = Path("app/static/steelshelf.css").read_text()
    phone = css.split("@media (max-width: 720px)")[1].split("}\n}")[0]
    assert ".sorts" in phone and "mask-image" in phone


# --- the side column ---------------------------------------------------------------


def test_most_valuable_is_the_top_five_priced():
    many = ITEMS + [row(id=10 + i, title=f"T{i}", latest_median=float(i)) for i in range(8)]
    top = shelf.most_valuable(many)
    assert [it["latest_median"] for it in top] == [99.0, 40.0, 7.0, 6.0, 5.0]
    assert all(it["latest_median"] is not None for it in shelf.most_valuable(ITEMS))
    assert len(shelf.most_valuable(ITEMS)) == 3  # Heat is unpriced


def fetch(item_id, median, at, currency="USD"):
    return {"item_id": item_id, "median": median, "currency": currency, "fetched_at": at}


def test_value_over_time_keeps_each_items_latest_and_one_point_a_day(monkeypatch):
    import time
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    history = [
        fetch(1, 10.0, "2026-07-01 10:00:00"),
        fetch(2, 20.0, "2026-07-01 11:00:00"),
        fetch(1, 15.0, "2026-08-01 10:00:00"),   # item 1 repriced
        fetch(3, 99.0, "2026-08-01 11:00:00", "GBP"),  # another currency: not summed
        fetch(2, None, "2026-09-01 10:00:00"),   # nothing listed: out of the total
    ]
    assert shelf.value_over_time(history, "USD") == [
        ("2026-07-01 11:00:00", 30.0),
        ("2026-08-01 10:00:00", 35.0),
        ("2026-09-01 10:00:00", 15.0),
    ]
    assert shelf.value_over_time([], "USD") == []
    monkeypatch.delenv("TZ")
    time.tzset()


def test_side_column_lists_the_top_and_draws_the_line(admin, stub_pricing):  # noqa: F811
    stub_pricing(StubSource())
    loc = post_item(admin, data={"title": "Alien"}).headers["location"]
    admin.post(f"{loc}/value", auth=AUTH)
    body = admin.get("/").text
    side = body.split('<aside class="side">')[1].split("</aside>")[0]
    assert "Most valuable" in side and f'href="{loc}"' in side and "35 USD" in side
    assert "Shelf value over time" in side and "a line once prices are fetched" in side
    assert "<polyline" not in side
    with db.connect(settings_module.settings.database_path) as conn:
        conn.execute("UPDATE valuations SET fetched_at = '2026-01-01 12:00:00'")
        conn.commit()
    admin.post(f"{loc}/value", auth=AUTH)
    side = admin.get("/").text.split('<aside class="side">')[1]
    assert '<polyline class="trend"' in side and "a line once" not in side


# --- search ----------------------------------------------------------------------------


def test_search_matches_every_word_in_any_field_ignoring_case():
    items = ITEMS + [row(id=9, title="Dune", edition="Steelbook #12/500", notes="dented corner")]
    assert [it["id"] for it in shelf.search(items, "best buy")] == [1, 3]
    assert [it["id"] for it in shelf.search(items, "BEST 4k heat")] == [3]
    assert [it["id"] for it in shelf.search(items, "uk")] == [2]
    assert [it["id"] for it in shelf.search(items, "dent")] == [9]
    assert [it["id"] for it in shelf.search(items, "#12/500")] == [9]
    assert shelf.search(items, "  ") == items and shelf.search(items, None) == items
    assert shelf.search(items, "zzz") == []


def test_search_route_keeps_the_sort_and_counts_the_matches(admin):
    for title, retailer in (("Alien", "Zavvi"), ("Heat", "Best Buy"), ("Jaws", "Best Buy")):
        post_item(admin, data={"title": title, "retailer": retailer})
    body = admin.get("/?sort=title&q=best").text
    assert '<input type="hidden" name="sort" value="title">' in body
    assert 'name="q" value="best"' in body
    assert "<strong>2 of 3</strong> match" in body and 'href="/?sort=title">clear</a>' in body
    shelf_html = body.split('class="shelves"')[1].split("</ul>")[0]
    assert shelf_html.count('class="spine ') == 2 and "Alien" not in shelf_html
    assert 'href="/?sort=label&amp;q=best"' in body  # the chips keep the search
    assert "3 steelbooks" in body  # the summary is the whole shelf

    body = admin.get("/?sort=label&q=nothing-like-it").text
    assert "<strong>0 of 3</strong>" in body
    assert 'class="bookcase"' in body and "Nothing on the shelf matches" in body
