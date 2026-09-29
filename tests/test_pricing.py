"""eBay pricing against a mocked transport — no network."""

import httpx
import pytest

from app import db
from app.pricing import (
    EbayActive,
    EbayKeyword,
    PricingError,
    SerpApiActive,
    SerpApiSold,
    title_match,
    is_opened,
    keyword_match,
    parse_prices,
    prefer_edition,
    record_sold,
    region_match,
    retailer_match,
    summarize,
    value_item,
)


def listing(value, currency="USD", title=None):
    s = {"price": {"value": str(value), "currency": currency}}
    if title is not None:
        s["title"] = title
    return s


class FakeEbay:
    """Serves the token and search endpoints; records what it was asked."""

    def __init__(self, summaries, search_status=(200,)):
        self.summaries = summaries
        self.search_status = list(search_status)
        self.token_calls = 0
        self.searches = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/identity/v1/oauth2/token":
            self.token_calls += 1
            return httpx.Response(
                200, json={"access_token": f"tok{self.token_calls}", "expires_in": 7200}
            )
        self.searches.append(request)
        status = self.search_status.pop(0) if self.search_status else 200
        if status != 200:
            return httpx.Response(status, text="nope")
        return httpx.Response(200, json={"itemSummaries": self.summaries})


def source(fake):
    return EbayActive("id", "secret", http=httpx.Client(transport=httpx.MockTransport(fake)))


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "p.db")
    db.init_db(path)
    with db.connect(path) as c:
        yield c


def add_item(conn, upc="012345678905"):
    cur = conn.execute("INSERT INTO items (title, upc) VALUES ('Alien', ?)", (upc,))
    conn.commit()
    return cur.lastrowid


def test_summarize_low_median_high():
    q = summarize("s", [30.0, 10.0, 20.0, 25.0], "USD")
    assert (q.low, q.median, q.high, q.n_listings) == (10.0, 22.5, 30.0, 4)


def test_summarize_empty_is_a_quote_of_nothing():
    q = summarize("s", [], "USD")
    assert (q.low, q.median, q.high, q.n_listings) == (None, None, None, 0)


def test_search_by_gtin_fixed_price_with_marketplace(conn):
    fake = FakeEbay([listing(20), listing(40), listing(30)])
    q = source(fake).quote(conn.execute(
        "SELECT * FROM items WHERE id = ?", (add_item(conn),)).fetchone())
    assert (q.low, q.median, q.high, q.n_listings, q.currency) == (20.0, 30.0, 40.0, 3, "USD")
    req = fake.searches[0]
    assert req.url.params["gtin"] == "012345678905"
    assert req.url.params["filter"] == "buyingOptions:{FIXED_PRICE}"
    assert req.headers["X-EBAY-C-MARKETPLACE-ID"] == "EBAY_US"
    assert req.headers["Authorization"] == "Bearer tok1"


def test_token_reused_across_quotes(conn):
    fake = FakeEbay([listing(10)])
    src = source(fake)
    item = conn.execute("SELECT * FROM items WHERE id = ?", (add_item(conn),)).fetchone()
    src.quote(item)
    src.quote(item)
    assert fake.token_calls == 1


def test_401_mints_a_new_token_once(conn):
    fake = FakeEbay([listing(10)], search_status=[401, 200])
    item = conn.execute("SELECT * FROM items WHERE id = ?", (add_item(conn),)).fetchone()
    q = source(fake).quote(item)
    assert q.n_listings == 1
    assert fake.token_calls == 2
    assert fake.searches[1].headers["Authorization"] == "Bearer tok2"


def test_other_currency_listings_dropped(conn):
    fake = FakeEbay([listing(10), listing(99, "GBP"), listing(20), {"title": "no price"}])
    item = conn.execute("SELECT * FROM items WHERE id = ?", (add_item(conn),)).fetchone()
    q = source(fake).quote(item)
    assert (q.low, q.high, q.n_listings) == (10.0, 20.0, 2)


def test_http_error_raises(conn):
    fake = FakeEbay([], search_status=[500])
    item = conn.execute("SELECT * FROM items WHERE id = ?", (add_item(conn),)).fetchone()
    with pytest.raises(PricingError, match="HTTP 500"):
        source(fake).quote(item)


def test_missing_keyset_raises(conn):
    src = EbayActive("", "", http=httpx.Client(transport=httpx.MockTransport(FakeEbay([]))))
    item = conn.execute("SELECT * FROM items WHERE id = ?", (add_item(conn),)).fetchone()
    with pytest.raises(PricingError, match="keyset missing"):
        src.quote(item)


def test_no_upc_raises(conn):
    item = conn.execute("SELECT * FROM items WHERE id = ?", (add_item(conn, upc=None),)).fetchone()
    with pytest.raises(PricingError, match="no UPC"):
        source(FakeEbay([])).quote(item)


def test_value_item_appends_never_overwrites(conn):
    item_id = add_item(conn)
    value_item(conn, item_id, source(FakeEbay([listing(10), listing(30)])))
    value_item(conn, item_id, source(FakeEbay([])))
    rows = conn.execute(
        "SELECT source, low, median, high, n_listings FROM valuations WHERE item_id = ?"
        " ORDER BY id", (item_id,)).fetchall()
    assert [tuple(r) for r in rows] == [
        ("ebay_active", 10.0, 20.0, 30.0, 2),
        ("ebay_active", None, None, None, 0),
    ]


def test_value_item_unknown_item(conn):
    with pytest.raises(PricingError, match="no item"):
        value_item(conn, 999, source(FakeEbay([])))


def keyword_source(fake):
    return EbayKeyword("id", "secret", http=httpx.Client(transport=httpx.MockTransport(fake)))


def item_row(conn, title="Alien", fmt=None, upc=None):
    cur = conn.execute(
        "INSERT INTO items (title, format, upc) VALUES (?, ?, ?)", (title, fmt, upc))
    conn.commit()
    return conn.execute("SELECT * FROM items WHERE id = ?", (cur.lastrowid,)).fetchone()


def test_keyword_match_needs_steelbook_and_every_title_word(conn):
    item = item_row(conn, title="The Thing")
    assert keyword_match("The Thing 4K Steelbook Zavvi", item)
    assert keyword_match("THING, the - Steel Book (Blu-ray)", item)
    assert not keyword_match("The Thing 4K UHD Blu-ray", item)  # not a steelbook
    assert not keyword_match("Alien steelbook", item)  # title word missing


def test_keyword_match_drops_lots_and_empty_cases(conn):
    item = item_row(conn)
    for title in ("Alien steelbook lot of 3", "Alien steelbook EMPTY case",
                  "Alien steelbook case only", "Alien steelbook no disc", "Alien steelbook bundle"):
        assert not keyword_match(title, item), title


def test_keyword_match_format(conn):
    uhd = item_row(conn, fmt="4K + Blu-ray")
    bd = item_row(conn, fmt="Blu-ray")
    dvd = item_row(conn, fmt="DVD")
    assert keyword_match("Alien 4K UHD steelbook", uhd)
    assert not keyword_match("Alien Blu-ray steelbook", uhd)
    assert keyword_match("Alien Blu-ray steelbook", bd)
    assert not keyword_match("Alien 4K steelbook", bd)
    assert keyword_match("Alien DVD steelbook", dvd)
    assert not keyword_match("Alien Blu-ray steelbook", dvd)


def test_keyword_search_when_no_upc(conn):
    fake = FakeEbay([
        listing(30, title="Alien 4K Steelbook Best Buy"),
        listing(50, title="Alien 4K UHD steelbook sealed"),
        listing(5, title="Alien 4K steelbook EMPTY"),
        listing(20, title="Alien 4K UHD Blu-ray"),
    ])
    q = keyword_source(fake).quote(item_row(conn, fmt="4K UHD"))
    assert (q.source, q.low, q.high, q.n_listings) == ("ebay_keyword", 30.0, 50.0, 2)
    req = fake.searches[0]
    assert req.url.params["q"] == "Alien steelbook 4K"
    assert req.url.params["category_ids"] == "617"
    assert req.url.params["filter"] == "buyingOptions:{FIXED_PRICE}"
    assert "gtin" not in req.url.params


def test_keyword_source_uses_upc_when_present(conn):
    fake = FakeEbay([listing(10, title="anything"), listing(20)])
    q = keyword_source(fake).quote(item_row(conn, upc="012345678905"))
    assert (q.source, q.n_listings) == ("ebay_active", 2)
    assert fake.searches[0].url.params["gtin"] == "012345678905"
    assert "q" not in fake.searches[0].url.params


def test_keyword_nothing_matching_is_a_quote_of_nothing(conn):
    fake = FakeEbay([listing(10, title="Aliens steelbook")])
    q = keyword_source(fake).quote(item_row(conn))
    assert (q.source, q.median, q.n_listings) == ("ebay_keyword", None, 0)


# --- sold prices entered by hand ---------------------------------------------


@pytest.mark.parametrize(
    "text, prices",
    [
        ("34.99, 41, 29.50", [34.99, 41.0, 29.5]),
        ("$34.99 $41\n$ 29.5", [34.99, 41.0, 29.5]),
        ("  25  ", [25.0]),
    ],
)
def test_parse_prices_takes_a_typed_list(text, prices):
    assert parse_prices(text) == prices


@pytest.mark.parametrize("text", ["", " , ", "34.99, abc", "12.345", "0", "-5", "30 USD"])
def test_parse_prices_refuses_anything_else(text):
    with pytest.raises(PricingError):
        parse_prices(text)


def test_record_sold_appends_a_manual_row(conn):
    item_id = add_item(conn)
    record_sold(conn, item_id, [30.0, 40.0, 50.0])
    record_sold(conn, item_id, [45.0])
    rows = conn.execute("SELECT * FROM valuations ORDER BY id").fetchall()
    assert [(r["source"], r["low"], r["median"], r["high"], r["n_listings"]) for r in rows] == [
        ("manual_sold", 30.0, 40.0, 50.0, 3),
        ("manual_sold", 45.0, 45.0, 45.0, 1),
    ]


# --- SerpApi sold lookups ----------------------------------------------------


def sold(title, raw="$30.00", extracted=30.0):
    return {"title": title, "price": {"raw": raw, "extracted": extracted}}


def serp(body, status=200, seen=None):
    def handler(request):
        if seen is not None:
            seen.append(request)
        return httpx.Response(status, json=body)

    return SerpApiSold("key", http=httpx.Client(transport=httpx.MockTransport(handler)))


def test_serpapi_sold_asks_for_sold_and_keeps_matching_dollar_prices(conn):
    seen = []
    source = serp({"organic_results": [
        sold("Alien 4K UHD Steelbook Best Buy", "$40.00", 40.0),
        sold("Alien 4K Steelbook sealed", "$60.00", 60.0),
        sold("Alien Blu-ray Steelbook", "$15.00", 15.0),          # wrong format
        sold("Alien 4K UHD", "$20.00", 20.0),                     # not a steelbook
        sold("Alien 4K Steelbook", "$30.00 to $50.00", None),     # a range
        sold("Alien 4K Steelbook", "C $45.00", 45.0),             # not dollars
    ]}, seen=seen)
    q = source.quote(item_row(conn, fmt="4K UHD"))
    assert (q.source, q.low, q.median, q.high, q.n_listings) == ("serpapi_sold", 40.0, 50.0, 60.0, 2)
    params = seen[0].url.params
    assert params["engine"] == "ebay" and params["show_only"] == "Sold"
    assert params["_nkw"] == "Alien steelbook 4K"


def test_serpapi_no_results_is_a_quote_of_nothing(conn):
    body = {"error": "eBay hasn't returned any results for this query."}
    q = serp(body).quote(item_row(conn, fmt="4K UHD"))
    assert q.n_listings == 0 and q.median is None


@pytest.mark.parametrize("body, status", [
    ({"error": "Invalid API key."}, 401),
    ({"error": "Your account has run out of searches."}, 200),
])
def test_serpapi_errors_raise(conn, body, status):
    with pytest.raises(PricingError, match="SerpApi search failed"):
        serp(body, status).quote(item_row(conn, fmt="4K UHD"))


def test_serpapi_without_a_key_raises_before_calling(conn):
    with pytest.raises(PricingError, match="SERPAPI_KEY"):
        SerpApiSold("").quote(item_row(conn, fmt="4K UHD"))


def edition_row(conn, retailer=None, region=None):
    cur = conn.execute(
        "INSERT INTO items (title, format, retailer, region) VALUES ('Alien', '4K UHD', ?, ?)",
        (retailer, region))
    conn.commit()
    return conn.execute("SELECT * FROM items WHERE id = ?", (cur.lastrowid,)).fetchone()


def test_retailer_match_spaced_joined_and_noise_words(conn):
    best_buy = edition_row(conn, retailer="Best Buy")
    assert retailer_match("Alien 4K Steelbook Best Buy Exclusive", best_buy)
    assert retailer_match("Alien 4K Steelbook BestBuy", best_buy)
    assert not retailer_match("Alien 4K Steelbook buy now", best_buy)
    amazon = edition_row(conn, retailer="Amazon.com Exclusive")
    assert retailer_match("Alien 4K Steelbook Amazon", amazon)
    assert not retailer_match("Alien 4K Steelbook", edition_row(conn))


def test_region_match_aliases_and_never_us(conn):
    assert region_match("Alien 4K Steelbook German Import", edition_row(conn, region="DE"))
    assert region_match("Alien 4K Steelbook UK Zavvi", edition_row(conn, region="uk"))
    assert not region_match("Alien 4K Steelbook", edition_row(conn, region="UK"))
    assert not region_match("Alien 4K Steelbook US", edition_row(conn, region="US"))
    assert not region_match("Alien 4K Steelbook", edition_row(conn))


def test_prefer_edition_tiers_then_everything(conn):
    item = edition_row(conn, retailer="Zavvi", region="UK")
    sales = [("Zavvi UK", 90.0), ("Zavvi UK", 95.0), ("Zavvi", 80.0),
             ("UK", 70.0), ("plain", 20.0), ("plain", 25.0)]
    assert prefer_edition(sales, item, minimum=2) == ([90.0, 95.0], "retailer+region")
    assert prefer_edition(sales, item, minimum=3) == ([90.0, 95.0, 80.0], "retailer")
    assert prefer_edition(sales, item, minimum=4) == ([p for _, p in sales], "all")
    region_only = [("UK", 1.0), ("UK", 2.0), ("UK", 3.0), ("plain", 4.0)]
    assert prefer_edition(region_only, item) == ([1.0, 2.0, 3.0], "region")


def test_prefer_edition_without_retailer_or_region_keeps_all(conn):
    sales = [("Zavvi UK", 90.0), ("a", 1.0), ("b", 2.0), ("c", 3.0)]
    assert prefer_edition(sales, edition_row(conn)) == ([90.0, 1.0, 2.0, 3.0], "all")


def test_serpapi_sold_narrows_to_the_items_retailer(conn):
    source = serp({"organic_results": [
        sold("Alien 4K Steelbook Best Buy", "$40.00", 40.0),
        sold("Alien 4K Steelbook BestBuy exclusive", "$42.00", 42.0),
        sold("Alien 4K UHD Steelbook Best Buy sealed", "$44.00", 44.0),
        sold("Alien 4K Steelbook Zavvi UK", "$120.00", 120.0),
        sold("Alien 4K Steelbook Mondo", "$150.00", 150.0),
    ]})
    q = source.quote(edition_row(conn, retailer="Best Buy", region="US"))
    assert (q.low, q.median, q.high, q.n_listings) == (40.0, 42.0, 44.0, 3)


@pytest.mark.parametrize("exc, message", [
    (httpx.ReadTimeout("timed out"), "no answer in time"),
    (httpx.ConnectError("refused"), "request failed"),
])
def test_value_item_network_failure_is_a_pricing_error(conn, exc, message):
    def handler(request):
        raise exc

    source = SerpApiSold("key", http=httpx.Client(transport=httpx.MockTransport(handler)))
    item_id = item_row(conn, fmt="4K UHD")["id"]
    with pytest.raises(PricingError, match=f"serpapi_sold: {message}"):
        value_item(conn, item_id, source)
    assert conn.execute("SELECT COUNT(*) FROM valuations").fetchone()[0] == 0


@pytest.mark.parametrize("listing", [
    "The Batman 4K STEELBOOK (UHD + Blu-Ray) Riddler Variant",
    'The Batman "Best Buy Ed" (4k Ultra HD + Blu-ray) Steelbook',
    "DC The Batman 4K SteelBook UHD Blu-ray [2022]",
    "(Korean Import) The Batman, steelbook Fullslip",
    "The Batman 2022 Steelbook (4K Blu-ray)",
    "The Batman DC Best Buy 4K Steelbook",
    "BATMAN, THE - Steelbook",
    "The Batman",
])
def test_title_match_takes_the_film(listing):
    assert title_match(listing, "The Batman")


@pytest.mark.parametrize("listing", [
    "Batman Begins - Steelbook [4K UHD]",
    "Batman v Superman Ultimate Edition 4K Steelbook",
    "Batman 1989 4K Ultra HD Blu-ray + SteelBook",
    "The Dark Knight Trilogy STEELBOOK batman superhero",
    "Batman THE DARK KNIGHT RISES (2012) 4K Steelbook",
    "The Batman Returns steelbook",
])
def test_title_match_refuses_other_films(listing):
    assert not title_match(listing, "The Batman")


def test_title_match_sequels_colons_and_ampersands():
    assert not title_match("Alien Covenant steelbook", "Alien")
    assert not title_match("Alien: Covenant steelbook", "Alien")
    assert not title_match("Toy Story 2 4K steelbook", "Toy Story")
    assert not title_match("Spider-Man: Homecoming steelbook", "Spider-Man")
    assert title_match("Spider-Man 4K Steelbook Manta Lab", "Spider-Man")
    assert title_match("Toy Story 2 4K steelbook", "Toy Story 2")
    assert title_match("Willy Wonka and the Chocolate Factory 4K steelbook",
                       "Willy Wonka & the Chocolate Factory")
    assert not title_match("anything", "")


def test_serpapi_active_asks_without_sold_filter_and_drops_auctions(conn):
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"organic_results": [
            {**sold("Alien 4K Steelbook", "$40.00", 40.0), "buying_format": "buy_it_now"},
            {**sold("Alien 4K Steelbook", "$50.00", 50.0), "buying_format": "accepts_offers"},
            {**sold("Alien 4K Steelbook", "$5.00", 5.0), "buying_format": "auction"},
            {**sold("Alien 4K Steelbook", "$6.00", 6.0), "bids": {"count": 2}},
        ]})

    source = SerpApiActive("key", http=httpx.Client(transport=httpx.MockTransport(handler)))
    q = source.quote(item_row(conn, fmt="4K UHD"))
    assert (q.source, q.low, q.high, q.n_listings) == ("serpapi_active", 40.0, 50.0, 2)
    assert "show_only" not in seen[0].url.params
    assert seen[0].url.params["_nkw"] == "Alien steelbook 4K"


def opened_row(conn, condition):
    cur = conn.execute(
        "INSERT INTO items (title, format, condition) VALUES ('Alien', '4K UHD', ?)",
        (condition,))
    conn.commit()
    return conn.execute("SELECT * FROM items WHERE id = ?", (cur.lastrowid,)).fetchone()


def test_is_opened(conn):
    assert is_opened(opened_row(conn, "opened"))
    assert is_opened(opened_row(conn, "dented"))
    assert not is_opened(opened_row(conn, "sealed"))
    assert not is_opened(opened_row(conn, "New"))
    assert not is_opened(opened_row(conn, None))


def active(results):
    def handler(request):
        return httpx.Response(200, json={"organic_results": results})

    return SerpApiActive("key", http=httpx.Client(transport=httpx.MockTransport(handler)))


def listed(price, condition):
    return {**sold("Alien 4K Steelbook", f"${price}.00", float(price)), "condition": condition}


def test_opened_item_priced_from_used_listings_when_three(conn):
    results = [listed(100, "Brand New"), listed(110, "Brand New"),
               listed(60, "Pre-Owned"), listed(70, "Used"), listed(80, "Pre-Owned: Good"),
               listed(90, "New (Other)")]
    q = active(results).quote(opened_row(conn, "opened"))
    assert (q.source, q.low, q.median, q.high) == ("serpapi_active_used", 60.0, 70.0, 80.0)
    q = active(results).quote(opened_row(conn, "sealed"))
    assert (q.source, q.n_listings) == ("serpapi_active", 6)


def test_opened_item_with_too_few_used_takes_every_listing(conn):
    results = [listed(100, "Brand New"), listed(110, "Brand New"), listed(60, "Pre-Owned")]
    q = active(results).quote(opened_row(conn, "opened"))
    assert (q.source, q.n_listings) == ("serpapi_active", 3)


def test_sold_never_narrows_by_condition(conn):
    results = [listed(100, "Brand New"), listed(60, "Pre-Owned"), listed(70, "Pre-Owned"),
               listed(80, "Pre-Owned")]
    q = serp({"organic_results": results}).quote(opened_row(conn, "opened"))
    assert (q.source, q.n_listings) == ("serpapi_sold", 4)
