"""The film's year, the edition's own words, and the item's own eBay search."""

import httpx

from app import db, film
from app.film import Film, pick
from app.identify import parse
from app.pricing import SerpApiSold, edition_terms, keyword_match, prefer_edition, title_match

from tests.conftest import AUTH
from tests.test_items import post_item, rows
from tests.test_pricing import conn, sold  # noqa: F401


def item(conn, **kw):  # noqa: F811
    fields = {"title": "The Thing", "format": "4K UHD", **kw}
    item_id = db.insert_item(conn, fields)
    conn.commit()
    return db.get_item(conn, item_id)


def test_a_year_after_the_title_must_be_the_films():
    assert title_match("The Thing (1982) 4K Steelbook", "The Thing", 1982)
    assert title_match("The Thing 4K Steelbook", "The Thing", 1982)  # no year: still it
    assert not title_match("The Thing (2011) 4K Steelbook", "The Thing", 1982)
    assert not title_match("THE THING 2011 steelbook", "The Thing", 1982)
    assert title_match("The Thing (2011) 4K Steelbook", "The Thing")  # year unknown


def test_keyword_match_uses_the_items_year(conn):  # noqa: F811
    thing = item(conn, year=1982)
    assert keyword_match("The Thing 1982 4K Steelbook", thing)
    assert not keyword_match("The Thing 2011 4K UHD Steelbook", thing)


def test_edition_terms_from_keywords_else_the_edition():
    assert edition_terms({"edition_keywords": "Manta Lab, E097 , ", "edition": "x"}) == [
        "Manta Lab", "E097"]
    assert edition_terms({"edition_keywords": None,
                          "edition": "Steelbook (Box set, Mondo #041, #710/1000, cat. 12345)"}
                         ) == ["Box set", "Mondo #041"]
    assert edition_terms({"edition": "Steelbook (Limited)"}) == []


def test_listings_naming_the_edition_words_come_first(conn):  # noqa: F811
    mondo = item(conn, edition="Steelbook (Mondo #041)", retailer="Best Buy")
    sales = [("The Thing 4K Steelbook Mondo 041", 90.0),
             ("The Thing 4K Steelbook MONDO #041 sealed", 110.0),
             ("The Thing 4K Steelbook Mondo #012", 40.0),
             ("The Thing 4K Steelbook Best Buy", 30.0)]
    assert prefer_edition(sales, mondo, minimum=1) == ([90.0, 110.0], "keywords")


def test_the_items_own_search_goes_before_the_title(conn):  # noqa: F811
    asked = []

    def handler(request):
        q = request.url.params["_nkw"]
        asked.append(q)
        found = {"The Thing steelbook Mondo": [sold("The Thing 4K Steelbook Mondo", "$90.00",
                                                     90.0)]}
        return httpx.Response(200, json={"organic_results": found.get(q, [])})

    source = SerpApiSold("k", http=httpx.Client(transport=httpx.MockTransport(handler)))
    q = source.quote(item(conn, search_query="The Thing steelbook Mondo"))
    assert asked == ["The Thing steelbook Mondo"] and q.median == 90.0
    asked.clear()
    source.quote(item(conn, search_query="The Thing steelbook Zavvi"))
    assert asked == ["The Thing steelbook Zavvi", "The Thing steelbook 4K"]


def test_tmdb_picks_the_items_year_among_remakes():
    results = [{"id": 2, "title": "The Thing", "release_date": "2011-10-14", "popularity": 50},
               {"id": 1, "title": "The Thing", "release_date": "1982-06-25", "popularity": 40}]
    assert pick("The Thing", results)["id"] == 2
    assert pick("The Thing", results, 1982)["id"] == 1


def test_items_matched_before_years_get_theirs(conn):  # noqa: F811
    old = db.insert_item(conn, {"title": "Alien"})
    db.set_film(conn, old, Film(348, "Horror", "Ridley Scott"))
    conn.commit()

    class Years:
        def year(self, tmdb_id):
            return {348: 1979}[tmdb_id]

    assert film.fill_years(conn, Years()) == 1
    assert db.get_item(conn, old)["year"] == 1979


def test_identify_reads_the_year_keywords_and_search():
    found = parse({"title": "La La Land", "year": "2016", "edition_keywords": "Manta Lab, E097",
                   "search_query": "La La Land steelbook Manta Lab"})
    assert found.values["year"] == 2016
    assert found.values["edition_keywords"] == "Manta Lab, E097"
    bad = parse({"title": "Alien", "year": "late 70s"})
    assert bad.values["year"] is None and "year" in bad.doubts[0]


def test_the_form_saves_them_and_refuses_a_bad_year(admin):
    post_item(admin, data={"year": "1982", "edition_keywords": "Mondo #041",
                           "search_query": "The Thing steelbook Mondo"})
    (saved,) = rows("SELECT year, edition_keywords, search_query FROM items")
    assert tuple(saved) == (1982, "Mondo #041", "The Thing steelbook Mondo")
    r = post_item(admin, data={"year": "82"})
    assert r.status_code == 422 and "year is four digits" in r.text
    assert 'name="edition_keywords"' in admin.get("/add", auth=AUTH).text
