"""The listings behind a price: stored with the valuation, shown, and set aside by key."""

import httpx

from app import db
from app.pricing import (
    EbayActive,
    Listing,
    Quote,
    SerpApiActive,
    excluded_keys,
    toggle_listing,
    value_item,
    without_excluded,
)

from tests.conftest import AUTH
from tests.test_items import post_item, rows, stub_pricing  # noqa: F401
from tests.test_pricing import FakeEbay, conn, item_row, listing  # noqa: F401


def serp_active(results):
    def handler(request):
        return httpx.Response(200, json={"organic_results": results})

    return SerpApiActive("key", http=httpx.Client(transport=httpx.MockTransport(handler)))


def ask(title, price, item="", condition="Pre-owned"):
    return {"title": title, "price": {"raw": f"${price:.2f}", "extracted": price},
            "condition": condition, "link": f"https://www.ebay.com/itm/{item}?x=1",
            "buying_format": "buy_it_now"}


ASKS = [
    ask("Alien 4K Steelbook", 40.0, "111111111"),
    ask("Alien 4K UHD Steelbook sealed", 60.0, "222222222"),
    ask("Alien 4K Steelbook Zavvi", 90.0, "333333333"),
]


# --- the quote carries its listings --------------------------------------------


def test_serpapi_quote_carries_listings_keyed_by_ebay_item_id(conn):  # noqa: F811
    q = serp_active(ASKS).quote(item_row(conn, fmt="4K UHD"))
    assert q.n_listings == 3 and q.median == 60.0
    assert [ls.key for ls in q.listings] == ["111111111", "222222222", "333333333"]
    assert q.listings[0] == Listing("Alien 4K Steelbook", 40.0, "111111111", "Pre-owned",
                                    "https://www.ebay.com/itm/111111111?x=1")


def test_browse_quote_carries_listings_keyed_by_item_id(conn):  # noqa: F811
    s = listing(25.0, title="Alien 4K Steelbook")
    s.update({"itemId": "v1|555|0", "condition": "New", "itemWebUrl": "https://ebay.com/x"})
    fake = FakeEbay([s])
    src = EbayActive("id", "secret", http=httpx.Client(transport=httpx.MockTransport(fake)))
    q = src.quote(item_row(conn, upc="012345678905"))
    assert q.listings == (Listing("Alien 4K Steelbook", 25.0, "v1|555|0", "New",
                                  "https://ebay.com/x"),)


def test_without_excluded_recounts_but_keeps_every_listing():
    q = Quote("s", 40.0, 60.0, 90.0, 3, "USD", tuple(
        Listing(t, p, k) for t, p, k in (("a", 40.0, "1"), ("b", 60.0, "2"), ("c", 90.0, "3"))))
    assert without_excluded(q, set()) is q
    r = without_excluded(q, {"3"})
    assert (r.low, r.median, r.high, r.n_listings) == (40.0, 50.0, 60.0, 2)
    assert r.listings == q.listings
    assert without_excluded(q, {"1", "2", "3"}).n_listings == 0


# --- stored, excluded, re-counted ---------------------------------------------------


class Src:
    name = "serpapi_active"

    def __init__(self, results):
        self.inner = serp_active(results)

    def quote(self, item):
        return self.inner.quote(item)


def test_value_item_stores_listings_and_later_fetches_skip_excluded_keys(conn):  # noqa: F811
    item = item_row(conn, fmt="4K UHD")
    vid = value_item(conn, item["id"], Src(ASKS))
    stored = db.valuation_listings(conn, vid)
    assert [(r["key"], r["price"], r["excluded"]) for r in stored] == [
        ("333333333", 90.0, 0), ("222222222", 60.0, 0), ("111111111", 40.0, 0)]

    zavvi = next(r for r in stored if r["key"] == "333333333")
    new_id = toggle_listing(conn, item["id"], zavvi["id"])
    assert new_id and excluded_keys(conn, item["id"]) == {"333333333"}
    v = conn.execute("SELECT * FROM valuations WHERE id = ?", (new_id,)).fetchone()
    assert (v["source"], v["low"], v["median"], v["high"], v["n_listings"]) == (
        "serpapi_active", 40.0, 50.0, 60.0, 2)
    # Every row of that key is flagged, and the re-count keeps it, flagged, last.
    assert [r["excluded"] for r in db.valuation_listings(conn, new_id)] == [0, 0, 1]
    assert conn.execute("SELECT excluded FROM listings WHERE id = ?", (zavvi["id"],)).fetchone()[0]

    # The next fetch returns the same three, and the flagged one stays out.
    vid3 = value_item(conn, item["id"], Src(ASKS))
    v3 = conn.execute("SELECT * FROM valuations WHERE id = ?", (vid3,)).fetchone()
    assert (v3["median"], v3["n_listings"]) == (50.0, 2)
    assert [(r["key"], r["excluded"]) for r in db.valuation_listings(conn, vid3)][-1] == (
        "333333333", 1)

    # Toggling it back counts it again.
    flagged = next(r for r in db.valuation_listings(conn, vid3) if r["excluded"])
    vid4 = toggle_listing(conn, item["id"], flagged["id"])
    assert excluded_keys(conn, item["id"]) == set()
    assert conn.execute("SELECT n_listings FROM valuations WHERE id = ?", (vid4,)).fetchone()[0] == 3
    assert toggle_listing(conn, item["id"] + 1, flagged["id"]) is None


# --- the page and the route ---------------------------------------------------------


def test_item_page_lists_the_listings_and_the_toggle_sets_one_aside(admin, stub_pricing):  # noqa: F811
    stub_pricing(Src(ASKS))
    loc = post_item(admin, data={"format": "4K UHD", "upc": ""}).headers["location"]
    admin.post(f"{loc}/value", auth=AUTH)
    page = admin.get(loc).text
    assert "Listings behind this price" in page
    assert 'href="https://www.ebay.com/itm/333333333?x=1"' in page
    assert page.count("Not this one") == 3 and "Count it" not in page

    (zavvi,) = rows("SELECT id FROM listings WHERE key = '333333333'")
    r = admin.post(f"{loc}/listings/{zavvi['id']}/toggle", follow_redirects=False)
    assert r.status_code == 401  # admin only
    r = admin.post(f"{loc}/listings/{zavvi['id']}/toggle", auth=AUTH, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"{loc}#listings"
    page = admin.get(loc).text
    assert "50.00 USD" in page  # re-counted from the two left
    assert page.count("Not this one") == 2 and page.count("Count it") == 1
    assert "set aside" in page and 'class="out"' in page
    assert "~50.00" in admin.get("/").text

    assert admin.post(f"{loc}/listings/999/toggle", auth=AUTH).status_code == 404
    assert admin.post("/items/999/listings/1/toggle", auth=AUTH).status_code == 404
