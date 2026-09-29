"""Pricing sources. Each one turns an item into a Quote; `value_item` appends it.

`EbayActive` is the low / median / high of current Buy It Now asks from
eBay's Browse API, searched by UPC. `EbayKeyword` is the same, but falls back
to a keyword search on the title when an item has no UPC, and filters the
noisier results down to steelbooks of that title and format. Both are a
floor, not a sold price — README § Pricing source has the decision and the
order alternatives would be tried in. A new source implements
`PricingSource` and nothing else changes.

Every fetched quote carries the listings it was computed from (`Quote.listings`),
which `_append` stores beside the valuation so the item page can show them. A
listing the owner marks "not this one" is remembered by its eBay id
(`db.excluded_keys`) and left out of the median on every later fetch.

`SerpApiSold` and `SoldComps` are the sold sources: eBay's sold search, as
scraped by SerpApi's eBay engine or by SoldComps. Each is a scraper at one
remove, so per the repo rule both ship disabled (`SOLD_LOOKUP_ENABLED`);
SoldComps is used when it has a key. `SerpApiActive` is the same engine's
current asks, used for asks only when there is no eBay keyset and that same
switch is on. `record_sold` is the one row not fetched: sold prices looked up on
eBay's site by hand and typed in, saved as `source = 'manual_sold'`.

Nothing here runs on a page render: callers are a POST handler or a job.
"""

import logging
import re
import sqlite3
import statistics
import time
from dataclasses import dataclass, replace
from typing import NamedTuple, Protocol

import httpx

log = logging.getLogger("steelshelf.pricing")


class PricingError(Exception):
    """A source could not produce a quote (no credentials, no UPC, HTTP failure)."""


class Listing(NamedTuple):
    """One eBay listing a quote counted. `key` is eBay's item id, stable across fetches."""

    title: str
    price: float
    key: str = ""
    condition: str = ""
    url: str = ""


@dataclass(frozen=True)
class Quote:
    source: str
    low: float | None
    median: float | None
    high: float | None
    n_listings: int
    currency: str
    listings: tuple[Listing, ...] = ()  # the ones the summary was computed from


class PricingSource(Protocol):
    name: str

    def quote(self, item: sqlite3.Row) -> Quote: ...


def summarize(
    source: str, prices: list[float], currency: str, listings: list[Listing] | tuple = ()
) -> Quote:
    """Low / median / high of a price list. An empty list is a quote too: nothing listed."""
    if not prices:
        return Quote(source, None, None, None, 0, currency, tuple(listings))
    return Quote(
        source,
        min(prices),
        round(statistics.median(prices), 2),
        max(prices),
        len(prices),
        currency,
        tuple(listings),
    )


def from_listings(source: str, listings: list[Listing] | tuple, currency: str) -> Quote:
    return summarize(source, [ls.price for ls in listings], currency, listings)


class EbayActive:
    """Browse API `item_summary/search?gtin=`, fixed-price listings only.

    Mints its own application token (client credentials) and reuses it until
    a minute before it expires. Item price only — shipping is not added, which
    keeps the number a floor.
    """

    name = "ebay_active"
    API = "https://api.ebay.com"
    SCOPE = "https://api.ebay.com/oauth/api_scope"
    LIMIT = 200  # Browse API page maximum; one page covers any single UPC

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        marketplace_id: str = "EBAY_US",
        http: httpx.Client | None = None,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.marketplace_id = marketplace_id
        self.http = http or httpx.Client(timeout=15)
        self._token: str | None = None
        self._token_expires = 0.0

    def _get_token(self) -> str:
        if self._token and time.monotonic() < self._token_expires:
            return self._token
        if not (self.client_id and self.client_secret):
            raise PricingError("eBay keyset missing: set EBAY_CLIENT_ID and EBAY_CLIENT_SECRET")
        r = self.http.post(
            f"{self.API}/identity/v1/oauth2/token",
            auth=(self.client_id, self.client_secret),
            data={"grant_type": "client_credentials", "scope": self.SCOPE},
        )
        if r.status_code != 200:
            raise PricingError(f"eBay token request failed: HTTP {r.status_code} {r.text[:200]}")
        body = r.json()
        self._token = body["access_token"]
        self._token_expires = time.monotonic() + int(body.get("expires_in", 7200)) - 60
        return self._token

    def _get(self, params: dict) -> httpx.Response:
        return self.http.get(
            f"{self.API}/buy/browse/v1/item_summary/search",
            params={"filter": "buyingOptions:{FIXED_PRICE}", "limit": self.LIMIT, **params},
            headers={
                "Authorization": f"Bearer {self._get_token()}",
                "X-EBAY-C-MARKETPLACE-ID": self.marketplace_id,
            },
        )

    def _search(self, params: dict) -> list[dict]:
        r = self._get(params)
        if r.status_code == 401:  # token revoked or expired early: mint once more
            self._token = None
            r = self._get(params)
        if r.status_code != 200:
            raise PricingError(f"eBay search failed: HTTP {r.status_code} {r.text[:200]}")
        return r.json().get("itemSummaries", [])

    @staticmethod
    def _listings(summaries: list[dict], what: str) -> tuple[list[Listing], str]:
        # Currency of the first priced listing; listings in any other currency are dropped
        # rather than mixed into the same median.
        currency = next((s["price"]["currency"] for s in summaries if "price" in s), "USD")
        listings = [
            Listing(s.get("title", ""), float(s["price"]["value"]),
                    key=s.get("itemId") or s.get("title", ""),
                    condition=s.get("condition", ""), url=s.get("itemWebUrl", ""))
            for s in summaries
            if s.get("price", {}).get("currency") == currency
        ]
        if len(listings) < len(summaries):
            log.info("%s: dropped %d listings without a %s price",
                     what, len(summaries) - len(listings), currency)
        return listings, currency

    def quote(self, item: sqlite3.Row) -> Quote:
        upc = (item["upc"] or "").strip()
        if not upc:
            raise PricingError(f"item {item['id']} has no UPC; eBay search is by UPC only")
        listings, currency = self._listings(self._search({"gtin": upc}), f"upc {upc}")
        # Named explicitly, not self.name: EbayKeyword reaches here for UPC items, and
        # those quotes are still exact-product asks.
        return from_listings(EbayActive.name, listings, currency)


_STOPWORDS = {"the", "a", "an", "of", "and"}
# A listing that is not one copy of the case with its discs.
_NOT_ONE_COPY = re.compile(
    r"\b(lots?|bundle|empty|replacement|case only|no discs?|discs? only)\b", re.I
)
_STEELBOOK = re.compile(r"\bsteel\s?book\b", re.I)
_4K = re.compile(r"\b(4k|uhd)\b", re.I)
_DVD = re.compile(r"\bdvd\b", re.I)


def _words(text: str) -> set[str]:
    return set(re.findall(r"[a-z0-9]+", text.lower().replace("'", ""))) - _STOPWORDS


# What may follow a film's title in a listing without making it another film:
# format, edition, retailer, condition, country. "Batman Begins" is not "Batman".
_AFTER_TITLE = {
    "4k", "uhd", "ultra", "hd", "blu", "ray", "bluray", "bd", "dvd", "digital", "3d",
    "steelbook", "steelbooks", "steel", "book", "limited", "edition", "collectors",
    "collector", "special", "anniversary", "exclusive", "lenticular", "fullslip", "full",
    "slip", "mondo", "best", "bestbuy", "zavvi", "walmart", "target", "amazon", "hmv",
    "dc", "marvel", "disney", "pixar", "new", "sealed", "mint", "used", "oop", "rare",
    "import", "region", "uk", "us", "german", "french", "italian", "korean", "japanese",
}
_YEAR = re.compile(r"(19|20)\d\d")
# Punctuation after a title that ends it: "The Batman (2022)", 'The Batman "Best Buy"'.
_TITLE_END = set('()[]{},|"-\u2013\u2014')


def _tokens(text: str) -> list[re.Match]:
    return list(re.finditer(r"[a-z0-9]+", text.lower().replace("'", "")))


def title_match(listing_title: str, title: str) -> bool:
    """Does the listing name this film, and not a sequel or another film sharing its words?

    The title's words must appear together and in order ("The Thing" also as "Thing,
    The"), then be followed by nothing, a closing mark, a year, or a word from
    `_AFTER_TITLE`. "and" is dropped from both, so "&" and "and" read the same.
    """
    want = [m.group() for m in _tokens(title) if m.group() != "and"]
    if not want:
        return False
    forms = [want] + ([want[1:] + ["the"]] if want[0] == "the" and len(want) > 1 else [])
    text = listing_title.lower().replace("'", "")
    toks = [m for m in _tokens(listing_title) if m.group() != "and"]
    words = [m.group() for m in toks]
    for form in forms:
        n = len(form)
        for i in range(len(words) - n + 1):
            if words[i:i + n] != form:
                continue
            if i + n == len(words):
                return True
            gap = text[toks[i + n - 1].end():toks[i + n].start()]
            nxt = words[i + n]
            if _TITLE_END & set(gap) or nxt in _AFTER_TITLE or _YEAR.fullmatch(nxt):
                return True
    return False


def keyword_match(listing_title: str, item: sqlite3.Row) -> bool:
    """Is this listing plausibly one copy of this item? Title, steelbook, format.

    The title is matched by `title_match`, so "The Batman" does not take "Batman
    Begins" and "Alien" does not take "Alien Covenant".
    """
    if not _STEELBOOK.search(listing_title) or _NOT_ONE_COPY.search(listing_title):
        return False
    if not title_match(listing_title, item["title"]):
        return False
    fmt = (item["format"] or "").lower()
    if "4k" in fmt:
        return bool(_4K.search(listing_title))
    if fmt == "blu-ray":
        return not _4K.search(listing_title)
    if fmt == "dvd":
        return bool(_DVD.search(listing_title))
    return True


class EbayKeyword(EbayActive):
    """`EbayActive`, plus a title keyword search for items with no UPC.

    The keyword search is scoped to eBay's DVDs & Blu-ray Discs category and
    filtered by `keyword_match`; its quotes are `source = 'ebay_keyword'` so
    they are never read as exact-product asks.
    """

    name = "ebay_keyword"
    CATEGORY = "617"  # DVDs & Blu-ray Discs, EBAY_US

    def quote(self, item: sqlite3.Row) -> Quote:
        if (item["upc"] or "").strip():
            return super().quote(item)
        q = f"{item['title']} steelbook"
        if "4k" in (item["format"] or "").lower():
            q += " 4K"
        summaries = self._search({"q": q, "category_ids": self.CATEGORY})
        kept = [s for s in summaries if keyword_match(s.get("title", ""), item)]
        log.info("keyword %r: kept %d of %d listings", q, len(kept), len(summaries))
        listings, currency = self._listings(kept, f"keyword {q!r}")
        return from_listings(self.name, listings, currency)


# Words that name no particular retailer or label: "Amazon.com Exclusive" is Amazon.
_RETAILER_NOISE = {"exclusive", "com", "co", "store", "shop"} | _STOPWORDS
# How a listing title says where a release is from. US is absent: on ebay.com it is
# the default, and a US listing seldom says so.
_REGION_WORDS = {
    "UK": {"uk", "british"},
    "DE": {"de", "german", "germany"},
    "FR": {"fr", "french", "france"},
    "IT": {"italian", "italy"},
    "ES": {"spanish", "spain"},
    "JP": {"jp", "japan", "japanese"},
    "KR": {"kr", "korea", "korean"},
    "CN": {"china", "chinese"},
    "CA": {"canada", "canadian"},
    "AU": {"au", "australia", "australian"},
}


def retailer_match(listing_title: str, item: sqlite3.Row) -> bool:
    """Does the listing name the item's retailer or label? "Best Buy" or "BestBuy"."""
    names = [w for w in re.findall(r"[a-z0-9]+", (item["retailer"] or "").lower())
             if w not in _RETAILER_NOISE]
    if not names:
        return False
    words = _words(listing_title)
    return set(names) <= words or "".join(names) in words


def region_match(listing_title: str, item: sqlite3.Row) -> bool:
    """Does the listing name the item's region of release? Never true for US or blank."""
    region = (item["region"] or "").strip().upper()
    if not region or region == "US":
        return False
    return bool(_REGION_WORDS.get(region, {region.lower()}) & _words(listing_title))


def prefer_edition_sales(sales: list, item: sqlite3.Row, minimum: int = 3) -> tuple[list, str]:
    """Narrow sales (anything (title, price, …)-shaped) to the item's own edition when
    enough of them say so.

    One film has several steelbooks and a title search takes them all. Sales that
    name the item's retailer and region, then its retailer, then its region, are
    tried in turn; the first set of at least `minimum` wins. Too few anywhere and
    every sale counts. Returns the sales kept and which set they are, for the log.
    """
    tiers = [
        ("retailer+region",
         lambda t: retailer_match(t, item) and region_match(t, item)),
        ("retailer", lambda t: retailer_match(t, item)),
        ("region", lambda t: region_match(t, item)),
    ]
    for label, keep in tiers:
        kept = [s for s in sales if keep(s[0])]
        if len(kept) >= minimum:
            return kept, label
    return list(sales), "all"


def prefer_edition(
    sales: list[tuple[str, float]], item: sqlite3.Row, minimum: int = 3
) -> tuple[list[float], str]:
    """`prefer_edition_sales`, returning just the prices."""
    kept, label = prefer_edition_sales(sales, item, minimum)
    return [s[1] for s in kept], label


def is_opened(item: sqlite3.Row) -> bool:
    """Is the item's copy opened, used or damaged — anything but sealed? Blank is unknown."""
    condition = (item["condition"] or "").strip().lower()
    return bool(condition) and "sealed" not in condition and "new" not in condition


def listing_used(result: dict) -> bool:
    """Is a SerpApi eBay result a used copy? "New (Other)" is unused, so not."""
    return str(result.get("condition", "")).lower().startswith(("pre-owned", "used"))


def search_terms(item: sqlite3.Row) -> str:
    """The keyword search every title-searched source runs: title, "steelbook", + "4K"."""
    q = f"{item['title']} steelbook"
    if "4k" in (item["format"] or "").lower():
        q += " 4K"
    return q


_EBAY_ITEM = re.compile(r"/itm/(?:[^/]+/)?(\d{9,})")


class SerpApiEbay:
    """eBay listings by title through SerpApi's eBay engine; subclasses pick which.

    The search is keyword-only; the results are filtered by `keyword_match`,
    like `EbayKeyword`'s, then narrowed to the
    item's retailer and region by `prefer_edition` when enough sales name them.
    Those two stay out of the query: many listings omit them, and one lookup is
    one search of the monthly quota. Item price only, US dollars only: a listing
    priced as a range (a variation listing) or in any other currency is dropped.
    """

    name = "serpapi_ebay"
    # Set, an opened item is priced from used listings alone when `minimum` of them
    # are there, and the quote carries this name so the page can tell the two apart.
    USED_NAME: str | None = None
    PARAMS: dict = {}
    API = "https://serpapi.com/search"
    NO_RESULTS = "hasn't returned any results"

    def __init__(self, api_key: str, http: httpx.Client | None = None):
        self.api_key = api_key
        # SerpApi scrapes eBay live; a 100-result sold search has taken over 30s.
        self.http = http or httpx.Client(timeout=60)

    def quote(self, item: sqlite3.Row) -> Quote:
        if not self.api_key:
            raise PricingError("SerpApi key missing: set SERPAPI_KEY")
        q = search_terms(item)
        r = self.http.get(self.API, params={
            "engine": "ebay", "ebay_domain": "ebay.com", "_nkw": q,
            "_ipg": 100, "api_key": self.api_key, **self.PARAMS,
        })
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        error = body.get("error", "")
        if self.NO_RESULTS in error:
            results = []
        elif r.status_code != 200 or error:
            raise PricingError(f"SerpApi search failed: HTTP {r.status_code} {error or r.text[:200]}")
        else:
            results = body.get("organic_results", [])
        kept = [s for s in results
                if keyword_match(s.get("title", ""), item) and self.keep(s)]
        priced = [
            s for s in kept
            if str(s.get("price", {}).get("raw", "")).startswith("$")
            and s["price"].get("extracted") is not None
        ]
        sales = [self._listing(s) for s in priced]
        name = self.name
        if self.USED_NAME and is_opened(item):
            used = [self._listing(s) for s in priced if listing_used(s)]
            if len(used) >= 3:
                sales, name = used, self.USED_NAME
        chosen, edition = prefer_edition_sales(sales, item)
        log.info("%s %r: kept %d of %d, %d priced, %d used (%s)",
                 name, q, len(kept), len(results), len(sales), len(chosen), edition)
        return from_listings(name, chosen, "USD")

    @staticmethod
    def _listing(result: dict) -> Listing:
        link = str(result.get("link", ""))
        m = _EBAY_ITEM.search(link)
        return Listing(
            result.get("title", ""), float(result["price"]["extracted"]),
            key=m.group(1) if m else (link or result.get("title", "")),
            condition=str(result.get("condition", "")),
            url=link,
        )

    def keep(self, result: dict) -> bool:
        return True


class SerpApiSold(SerpApiEbay):
    """Sold eBay listings (`show_only=Sold`); eBay's sold search covers about 90 days."""

    name = "serpapi_sold"
    PARAMS = {"show_only": "Sold"}


class SerpApiActive(SerpApiEbay):
    """Current eBay asks by title, for when there is no eBay keyset: a floor, like
    `EbayActive`'s. Auctions are dropped, since a bid so far is not an ask."""

    name = "serpapi_active"
    USED_NAME = "serpapi_active_used"

    def keep(self, result: dict) -> bool:
        return result.get("buying_format") != "auction" and "bids" not in result


class SoldComps:
    """Sold eBay listings through SoldComps (sold-comps.com), searched by title.

    The same search and filtering as `SerpApiSold` — `keyword_match`, then
    `prefer_edition_sales` — from another vendor's scrape of eBay's sold search,
    so it sits behind the same switch. Item price only (`soldPrice`, not
    `totalPrice`, which adds shipping), US dollars only. One lookup is one
    request of the month's quota whatever `count` is, so it asks for the most.
    """

    name = "soldcomps_sold"
    API = "https://api.sold-comps.com/v1/scrape"

    def __init__(self, api_key: str, http: httpx.Client | None = None):
        self.api_key = api_key
        # It scrapes eBay live: a median of 4-6s, and slower when eBay is.
        self.http = http or httpx.Client(timeout=60)

    def quote(self, item: sqlite3.Row) -> Quote:
        if not self.api_key:
            raise PricingError("SoldComps key missing: set SOLDCOMPS_KEY")
        q = search_terms(item)
        r = self.http.get(
            self.API,
            params={"keyword": q, "ebaySite": "ebay.com", "count": 200},
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
        if r.status_code != 200:
            # The live API answers {"error": ...}; its docs describe {code, message}.
            error = (body.get("error") or body.get("message") or body.get("code")
                     or r.text[:200])
            raise PricingError(f"SoldComps search failed: HTTP {r.status_code} {error}")
        results = body.get("items") or []
        kept = [s for s in results if keyword_match(s.get("title", ""), item)]
        sales = [self._listing(s) for s in kept if s.get("soldCurrency") == "USD"
                 and self._price(s) is not None]
        chosen, edition = prefer_edition_sales(sales, item)
        log.info("%s %r: kept %d of %d, %d priced, %d used (%s)",
                 self.name, q, len(kept), len(results), len(sales), len(chosen), edition)
        return from_listings(self.name, chosen, "USD")

    @staticmethod
    def _price(result: dict) -> float | None:
        try:
            price = float(result.get("soldPrice"))
        except (TypeError, ValueError):
            return None
        return price if price > 0 else None

    @classmethod
    def _listing(cls, result: dict) -> Listing:
        url = str(result.get("url", ""))
        return Listing(
            result.get("title", ""), cls._price(result),
            key=str(result.get("itemId") or url or result.get("title", "")),
            condition=str(result.get("condition") or ""),
            url=url,
        )


def value_item(conn: sqlite3.Connection, item_id: int, source: PricingSource) -> int:
    """Quote an item and append a `valuations` row. Never updates: history is the chart.

    Listings the owner has excluded before are left out of the summary but stored with
    the valuation, flagged, so the page still shows what was set aside.
    """
    return append_quote(conn, item_id, fetch_quote(conn, item_id, source))


def fetch_quote(conn: sqlite3.Connection, item_id: int, source: PricingSource) -> Quote:
    """An item's quote with its excluded listings left out of the summary; nothing written."""
    item = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    if item is None:
        raise PricingError(f"no item {item_id}")
    try:
        quote = source.quote(item)
    except httpx.TimeoutException as exc:
        raise PricingError(f"{source.name}: no answer in time, try again") from exc
    except httpx.HTTPError as exc:
        raise PricingError(f"{source.name}: request failed: {exc}") from exc
    return without_excluded(quote, excluded_keys(conn, item_id))


def append_quote(conn: sqlite3.Connection, item_id: int, quote: Quote,
                 via: str | None = None) -> int:
    """Append a fetched quote as a valuation; `via` names a job that fetched it."""
    return _append(conn, item_id, quote, via)


def excluded_keys(conn: sqlite3.Connection, item_id: int) -> set[str]:
    return {
        r[0] for r in conn.execute(
            "SELECT DISTINCT key FROM listings WHERE item_id = ? AND excluded = 1", (item_id,)
        )
    }


def without_excluded(quote: Quote, excluded: set[str]) -> Quote:
    """The quote summarised over its listings minus the excluded ones, all still attached."""
    if not quote.listings or not excluded:
        return quote
    counted = [ls for ls in quote.listings if ls.key not in excluded]
    if len(counted) == len(quote.listings):
        return quote
    return replace(from_listings(quote.source, counted, quote.currency),
                   listings=quote.listings)


def toggle_listing(conn: sqlite3.Connection, item_id: int, listing_id: int) -> int | None:
    """Flip one listing's exclusion for this item, then append the latest valuation
    re-counted without (or with) it. Returns the new valuation id, or None if the
    listing is not this item's."""
    row = conn.execute(
        "SELECT * FROM listings WHERE id = ? AND item_id = ?", (listing_id, item_id)
    ).fetchone()
    if row is None:
        return None
    now_excluded = 0 if row["excluded"] else 1
    conn.execute("UPDATE listings SET excluded = ? WHERE item_id = ? AND key = ?",
                 (now_excluded, item_id, row["key"]))
    latest = conn.execute(
        "SELECT * FROM valuations WHERE id = ?", (row["valuation_id"],)
    ).fetchone()
    listings = [
        Listing(r["title"], r["price"], r["key"], r["condition"] or "", r["url"] or "")
        for r in conn.execute("SELECT * FROM listings WHERE valuation_id = ? ORDER BY id",
                              (latest["id"],))
    ]
    quote = from_listings(latest["source"], listings, latest["currency"])
    return _append(conn, item_id, without_excluded(quote, excluded_keys(conn, item_id)))


MANUAL_SOLD = "manual_sold"
SOLD_SOURCES = (MANUAL_SOLD, "serpapi_sold", "soldcomps_sold")  # the sources that are sold prices, not asks
_PRICE = re.compile(r"\$?(\d+(?:\.\d{1,2})?)")


def parse_prices(text: str) -> list[float]:
    """Prices typed as a list: commas, spaces or newlines between, `$` optional.

    Raises PricingError on anything else, so a typo never lands as a sale.
    """
    tokens = [t for t in re.split(r"[,\s]+", text.replace("$ ", "$")) if t]
    prices = []
    for t in tokens:
        m = _PRICE.fullmatch(t)
        if m is None:
            raise PricingError(f"not a price: {t!r}")
        prices.append(float(m.group(1)))
    if not prices:
        raise PricingError("enter at least one sold price")
    if any(p <= 0 for p in prices):
        raise PricingError("a sold price must be above zero")
    return prices


def record_sold(conn: sqlite3.Connection, item_id: int, prices: list[float]) -> int:
    """Append a `valuations` row from sold prices entered by hand."""
    return _append(conn, item_id, summarize(MANUAL_SOLD, prices, "USD"))


def _append(conn: sqlite3.Connection, item_id: int, q: Quote, via: str | None = None) -> int:
    excluded = excluded_keys(conn, item_id) if q.listings else set()
    cur = conn.execute(
        "INSERT INTO valuations (item_id, source, low, median, high, n_listings, currency, via)"
        " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (item_id, q.source, q.low, q.median, q.high, q.n_listings, q.currency, via),
    )
    valuation_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO listings (valuation_id, item_id, key, title, price, condition, url,"
        " excluded) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        [(valuation_id, item_id, ls.key, ls.title, ls.price, ls.condition, ls.url,
          int(ls.key in excluded)) for ls in q.listings],
    )
    conn.commit()
    return valuation_id


class Confidence(NamedTuple):
    """How far to trust a price: 3 solid, 2 fair, 1 thin, with one sentence why."""

    level: int
    word: str
    sentence: str


def confidence(
    n: int | None, low: float | None, high: float | None, median: float | None,
    noun: str = "listing",
) -> Confidence | None:
    """The cue beside a price, from how many listings it counted and how far apart they sit.

    Four or fewer is thin whatever the spread. Twelve or more is solid when the
    range (high minus low) is no wider than three quarters of the median; a wider
    range caps the price at fair. None for a valuation that priced nothing.
    """
    if not n or median is None:
        return None
    many = f"{n} {noun}{'s' if n != 1 else ''}"
    spread = (high - low) / median if median and high is not None and low is not None else 0
    if n <= 4:
        return Confidence(1, "Thin", f"Only {many}; one more or less moves it a lot.")
    if spread > 0.75:
        return Confidence(2, "Fair", f"{many}, spread wide from {low:,.0f} to {high:,.0f}.")
    if n >= 12:
        return Confidence(3, "Solid", f"{many}, close together.")
    return Confidence(2, "Fair", f"{many}, close together but not many.")
