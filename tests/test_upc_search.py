"""Title-searched sources try the UPC first; asks narrow to a sealed copy's condition too."""

import httpx

from app.pricing import SerpApiActive, SerpApiSold, SoldComps

from tests.test_pricing import comp, conn, listed, opened_row, sold  # noqa: F401


def upc_item(conn, upc="012345678905", retailer=None):  # noqa: F811
    cur = conn.execute("INSERT INTO items (title, format, upc, retailer) VALUES"
                       " ('Alien', '4K UHD', ?, ?)", (upc, retailer))
    conn.commit()
    return conn.execute("SELECT * FROM items WHERE id = ?", (cur.lastrowid,)).fetchone()


def serp(cls, by_query):
    """A SerpApi double answering each `_nkw` from `by_query`; records the queries."""
    asked = []

    def handler(request):
        q = request.url.params["_nkw"]
        asked.append(q)
        return httpx.Response(200, json={"organic_results": by_query.get(q, [])})

    return cls("key", http=httpx.Client(transport=httpx.MockTransport(handler))), asked


def test_a_upc_search_that_finds_the_product_is_the_whole_quote(conn):  # noqa: F811
    source, asked = serp(SerpApiSold, {"012345678905": [
        sold("Alien (1979) 4K UHD + Blu-ray", "$50.00", 50.0),  # no "steelbook": still it
        sold("Alien 4K Steelbook Zavvi", "$60.00", 60.0),
        sold("Prometheus 4K Steelbook", "$20.00", 20.0),  # eBay padding: another film
        sold("Alien steelbook EMPTY case", "$5.00", 5.0),
    ]})
    q = source.quote(upc_item(conn, retailer="Best Buy"))
    assert asked == ["012345678905"]
    # Exact product: no narrowing to "Best Buy", though neither listing names it.
    assert (q.matched, q.n_listings, q.median, q.searches) == ("upc", 2, 55.0, 1)


def test_a_upc_search_that_finds_nothing_falls_back_to_the_title(conn):  # noqa: F811
    source, asked = serp(SerpApiActive, {"Alien steelbook 4K": [
        sold("Alien 4K Steelbook", "$40.00", 40.0)]})
    q = source.quote(upc_item(conn))
    assert asked == ["012345678905", "Alien steelbook 4K"]
    assert (q.matched, q.n_listings, q.searches) == ("all", 1, 2)


def test_no_upc_is_one_title_search(conn):  # noqa: F811
    source, asked = serp(SerpApiActive, {})
    q = source.quote(upc_item(conn, upc=None))
    assert asked == ["Alien steelbook 4K"] and (q.n_listings, q.searches) == (0, 1)


def test_soldcomps_searches_the_upc_first(conn):  # noqa: F811
    asked = []

    def handler(request):
        asked.append(request.url.params["keyword"])
        return httpx.Response(200, json={"items": [comp("Alien 4K Steelbook", "45.00")]})

    source = SoldComps("k", http=httpx.Client(transport=httpx.MockTransport(handler)))
    q = source.quote(upc_item(conn))
    assert asked == ["012345678905"] and (q.matched, q.median) == ("upc", 45.0)


def test_a_sealed_item_is_priced_from_new_asks_when_three(conn):  # noqa: F811
    results = [listed(100, "Brand New"), listed(110, "New"), listed(90, "New (Other)"),
               listed(60, "Pre-Owned"), listed(70, "Used")]
    source, _ = serp(SerpApiActive, {"Alien steelbook 4K": results})
    q = source.quote(opened_row(conn, "sealed"))
    assert (q.source, q.median, q.n_listings) == ("serpapi_active_new", 100.0, 3)
    few = [listed(100, "Brand New"), listed(60, "Pre-Owned"), listed(70, "Used")]
    source, _ = serp(SerpApiActive, {"Alien steelbook 4K": few})
    assert source.quote(opened_row(conn, "sealed")).source == "serpapi_active"
    source, _ = serp(SerpApiActive, {"Alien steelbook 4K": results})
    assert source.quote(opened_row(conn, None)).source == "serpapi_active"  # unknown
