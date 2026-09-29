"""How a price's listings matched the edition, and the range and confidence it is quoted with."""

from app.pricing import Quote, confidence, record_sold, spread, summarize, value_item

from tests.conftest import AUTH
from tests.test_items import post_item, stub_pricing  # noqa: F401
from tests.test_pricing import (  # noqa: F401
    FakeEbay,
    comp,
    conn,
    edition_row,
    keyword_source,
    listing,
    soldcomps,
)


def test_the_range_is_the_middle_half_from_five_prices():
    # Arrival's asks: one $965 listing among the film's $50 steelbooks.
    prices = [33.0, 45.0, 50.0, 52.0, 55.0, 60.0, 964.99]
    assert spread(prices) == (47.5, 57.5)
    q = summarize("s", prices, "USD")
    assert (q.low, q.median, q.high) == (47.5, 52.0, 57.5)
    assert spread([10.0, 20.0, 30.0, 40.0]) == (10.0, 40.0)  # four: low to high


def test_one_stray_listing_no_longer_knocks_confidence_down():
    prices = [48.0, 49.0, 50.0, 50.0, 51.0, 52.0, 52.0, 53.0, 54.0, 55.0, 56.0, 964.99]
    q = summarize("s", prices, "USD")
    assert confidence(q.n_listings, q.low, q.high, q.median).word == "Solid"


def test_a_film_wide_price_is_at_best_fair():
    solid = confidence(20, 30, 40, 35, matched="retailer")
    assert solid.word == "Solid"
    wide = confidence(20, 30, 40, 35, matched="all")
    assert wide[:2] == (2, "Fair") and "every steelbook of the film" in wide.sentence
    assert confidence(3, 30, 40, 35, matched="all").word == "Thin"


def test_ebay_keyword_narrows_to_the_edition_and_says_how(conn):  # noqa: F811
    item = edition_row(conn, retailer="Best Buy")
    fake = FakeEbay([
        listing(30, title="Alien 4K Steelbook Best Buy"),
        listing(32, title="Alien 4K Steelbook Best Buy exclusive"),
        listing(34, title="Alien 4K UHD Steelbook BestBuy"),
        listing(120, title="Alien 4K Steelbook Zavvi"),
    ])
    q = keyword_source(fake).quote(item)
    assert (q.n_listings, q.high, q.matched) == (3, 34.0, "retailer")
    everyone = keyword_source(FakeEbay([
        listing(30, title="Alien 4K Steelbook"), listing(120, title="Alien 4K Steelbook Zavvi"),
    ])).quote(item)
    assert (everyone.n_listings, everyone.matched) == (2, "all")


def test_the_match_is_stored_on_the_valuation(conn):  # noqa: F811
    item = edition_row(conn, retailer="Manta Lab")

    class Source:
        name = "soldcomps_sold"

        def quote(self, it):
            return soldcomps({"items": [comp("Alien 4K Steelbook Manta Lab", "600.00")]}
                             ).quote(it)

    value_item(conn, item["id"], Source())
    record_sold(conn, item["id"], [500.0])
    assert [r[0] for r in conn.execute("SELECT matched FROM valuations ORDER BY id")] == [
        "retailer", "typed"]


class FilmWide:
    name = "serpapi_active"

    def quote(self, item):
        return Quote("serpapi_active", 40.0, 52.0, 60.0, 56, "USD", matched="all")


def test_review_lists_the_film_wide_prices_and_the_item_page_says_so(admin, stub_pricing):  # noqa: F811
    stub_pricing(FilmWide())
    loc = post_item(admin, data={"title": "Arrival"}).headers["location"]
    admin.post(f"{loc}/value", auth=AUTH)
    page = admin.get(loc).text
    assert "Fair confidence." in page and "every steelbook of the film" in page
    assert "middle half from 40.00" in page
    review = admin.get("/review").text
    assert "Priced across the whole film" in review and "Arrival" in review
    assert "52 USD" in review and "no\n      retailer named" in review
