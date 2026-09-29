"""The /stats page: value by label, how sure the prices are, the breakdowns."""

from app import stats

from tests.conftest import AUTH
from tests.test_items import post_item, stub_pricing  # noqa: F401
from tests.test_shelf import row as shelf_row


def row(**kw):
    """A db.list_items row with every column stats reads; unpriced unless given."""
    base = {"condition": None, "latest_low": None, "latest_high": None, "latest_n": None,
            "latest_source": None, "latest_fetched_at": None}
    return shelf_row(**{**base, **kw})


def priced(**kw):
    base = {"latest_median": 30.0, "latest_low": 28.0, "latest_high": 32.0, "latest_n": 14,
            "latest_source": "ebay_keyword", "latest_fetched_at": "2026-09-25 12:00:00"}
    return row(**{**base, **kw})


ITEMS = [
    priced(id=1, title="Alien", retailer="Zavvi", format="Blu-ray", region="UK",
           condition="sealed", latest_median=40.0),
    priced(id=2, title="Heat", retailer="Best Buy", format="4K UHD", region="US",
           condition="opened", latest_n=3),                                  # thin
    priced(id=3, title="Jaws", retailer="Best Buy", format="4K + Blu-ray", region="US",
           latest_n=7, latest_source="manual_sold",
           latest_fetched_at="2026-09-26 01:00:00"),                        # fair, sold
    row(id=4, title="Dune", format="DVD", region="DE", condition="Opened, dented",
        latest_currency=None),                                              # unpriced
]


def test_value_by_label_is_largest_first_with_a_share_of_the_top():
    bars = stats.value_by_label(ITEMS)
    assert [(b.name, b.value, b.pct) for b in bars] == [
        ("Best Buy", 60.0, 100.0), ("Zavvi", 40.0, 66.7), ("No retailer named", None, 0.0)]


def test_an_unpriced_group_has_no_total_and_goes_last():
    items = [row(id=1, title="Heat", retailer="101 Films"),
             priced(id=2, title="Alien", retailer="Zavvi", latest_median=0.0),
             priced(id=3, title="Jaws", retailer="Mondo")]
    assert [(b.name, b.value) for b in stats.value_by_label(items)] == [
        ("Mondo", 30.0), ("Zavvi", 0.0), ("101 Films", None)]


def test_certainty_sorts_each_price_into_solid_fair_or_thin():
    c = stats.certainty(ITEMS)
    assert (c.solid, c.fair, c.thin, c.unpriced, c.sold) == (1, 1, 1, 1, 1)
    assert [it["title"] for it in c.thin_items] == ["Heat"]
    assert c.pct(c.solid) == 33.3
    assert stats.certainty([]).pct(0) == 0.0


def test_breakdowns_bucket_format_region_and_condition():
    b = stats.breakdowns(ITEMS)
    assert [(x.name, x.value) for x in b["Format"]] == [("4K", 2), ("Blu-ray", 1), ("DVD", 1)]
    assert [(x.name, x.value) for x in b["Condition"]] == [
        ("opened", 2), ("sealed", 1), ("not given", 1)]
    assert b["Format"][0].pct == 100.0 and b["Format"][1].pct == 50.0


def test_region_keeps_the_three_biggest_and_pools_the_rest():
    many = [row(id=i, region=r) for i, r in enumerate(
        ["US"] * 4 + ["UK"] * 3 + ["DE"] * 2 + ["FR", "JP"] + [None])]
    assert [(x.name, x.value) for x in stats.breakdowns(many)["Region"]] == [
        ("US", 4), ("UK", 3), ("DE", 2), ("other", 2), ("not given", 1)]


def test_as_of_is_the_newest_latest_price():
    assert stats.as_of(ITEMS) == "2026-09-26 01:00:00"
    assert stats.as_of([row(latest_fetched_at=None)]) is None


def test_stats_page_renders_and_is_linked(admin, stub_pricing):  # noqa: F811
    from tests.test_items import StubSource
    assert "Nothing on the shelf yet" in admin.get("/stats").text
    stub_pricing(StubSource())  # 7 listings, spread wide: fair
    loc = post_item(admin, data={"title": "Alien", "retailer": "Zavvi",
                                 "format": "4K UHD"}).headers["location"]
    admin.post(f"{loc}/value", auth=AUTH)
    post_item(admin, data={"title": "Heat"})
    page = admin.get("/stats")
    assert page.status_code == 200
    body = page.text
    assert "<h1>The shelf in numbers</h1>" in body
    assert "2 steelbooks\n    · asks as of" in body
    assert "Zavvi" in body and "35 USD" in body
    assert 'title="not priced">—</span>' in body  # Heat's unnamed group
    assert "<b>1</b> fair" in body and "1 not priced" in body
    assert "1 item names\n      no retailer" in body
    assert "Most valuable" not in body  # lives in the shelf's side column only
    assert 'href="/stats"' in admin.get("/").text
