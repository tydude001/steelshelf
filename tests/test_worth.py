"""What an item is worth: the one rule (`pricing.current_worth`) every page and job reads."""

from datetime import UTC, datetime, timedelta

import pytest

from app import db, reprice, shelf
from app.pricing import current_worth

NOW = "2026-10-25 08:00:00"


def v(id, source, median, at, currency="USD"):
    return {"id": id, "item_id": 1, "source": source, "median": median, "fetched_at": at,
            "currency": currency}


def test_a_fresh_sold_price_outranks_a_newer_ask():
    sold = v(1, "soldcomps_sold", 630.0, "2026-09-29 12:00:00")
    ask = v(2, "serpapi_active", 143.5, "2026-10-25 08:00:00")
    assert current_worth([ask, sold], NOW) is sold


def test_a_sold_price_older_than_ninety_days_gives_way_to_the_ask():
    sold = v(1, "serpapi_sold", 630.0, "2026-07-01 12:00:00")
    ask = v(2, "serpapi_active", 143.5, "2026-10-25 08:00:00")
    assert current_worth([ask, sold], NOW) is ask
    assert current_worth([sold], NOW) is sold  # stale, but nothing newer to take over


def test_only_the_newest_sold_price_counts():
    old_sold = v(1, "soldcomps_sold", 630.0, "2026-09-01 12:00:00")
    new_sold = v(2, "soldcomps_sold", 500.0, "2026-10-01 12:00:00")
    ask = v(3, "serpapi_active", 143.5, "2026-10-20 12:00:00")
    assert current_worth([ask, new_sold, old_sold], NOW) is new_sold


def test_a_sold_lookup_that_found_nothing_hands_back_to_the_asks():
    ask = v(1, "serpapi_active", 40.0, "2026-10-01 12:00:00")
    none_sold = v(2, "soldcomps_sold", None, "2026-10-20 12:00:00")
    assert current_worth([none_sold, ask], NOW) is ask


def test_an_ask_that_found_nothing_leaves_no_price():
    ask = v(1, "serpapi_active", 40.0, "2026-10-01 12:00:00")
    empty = v(2, "serpapi_active", None, "2026-10-20 12:00:00")
    assert current_worth([empty, ask], NOW) is None
    assert current_worth([], NOW) is None


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "w.db")
    db.init_db(path)
    c = db.connect(path)
    yield c
    c.close()


def ago(days: int) -> str:
    """A UTC stamp `days` before now: list_items judges freshness by the real clock."""
    return (datetime.now(UTC) - timedelta(days=days)).strftime("%Y-%m-%d %H:%M:%S")


def priced(conn, item_id, source, median, at, via=None):
    conn.execute("INSERT INTO valuations (item_id, source, median, low, high, n_listings,"
                 " fetched_at, via) VALUES (?, ?, ?, ?, ?, 1, ?, ?)",
                 (item_id, source, median, median, median, at, via))
    conn.commit()


def test_the_shelf_total_counts_a_sold_price_over_the_monthly_ask(conn):
    """La La Land's Manta Lab: SoldComps found its own sales at $630 on 29 Sep; the monthly
    re-price on 25 Oct found asks for the film's other steelbooks at $143.50."""
    lalaland = db.insert_item(conn, {"title": "La La Land", "retailer": "Manta Lab"})
    priced(conn, lalaland, "serpapi_active", 143.5, ago(35))
    priced(conn, lalaland, "soldcomps_sold", 630.0, ago(26))
    priced(conn, lalaland, "serpapi_active", 143.5, ago(0), via="monthly")
    (item,) = db.list_items(conn)
    assert (item["latest_median"], item["latest_source"]) == (630.0, "soldcomps_sold")
    history = db.valuation_history(conn)
    assert [total for _, total in shelf.value_over_time(history, "USD")] == [
        143.5, 630.0, 630.0]


def test_the_monthly_plan_skips_a_typed_price_even_behind_a_newer_ask(conn):
    typed = db.insert_item(conn, {"title": "Typed"})
    priced(conn, typed, "manual_sold", 40.0, "2026-09-01 12:00:00")
    looked_up = db.insert_item(conn, {"title": "Looked up"})
    priced(conn, looked_up, "soldcomps_sold", 60.0, "2026-09-01 12:00:00")
    assert reprice.planned(conn, "2026-09-25 08:00:00") == [looked_up]


def test_the_shelf_floor_splits_sold_from_asks_and_counts_paid_once():
    from app.main import _shelf_floor
    rows = [
        {"latest_median": 100.0, "latest_currency": "USD", "latest_source": "soldcomps_sold",
         "paid_price": 50.0},
        {"latest_median": 20.0, "latest_currency": "USD", "latest_source": "serpapi_active",
         "paid_price": None},
        {"latest_median": 30.0, "latest_currency": "GBP", "latest_source": "ebay_keyword",
         "paid_price": 25.0},
        {"latest_median": None, "latest_currency": None, "latest_source": None,
         "paid_price": 10.0},  # unpriced: its paid counts toward the largest total
    ]
    usd, gbp = _shelf_floor(rows)
    assert (usd["total"], usd["sold"], usd["n_sold"], usd["asks"]) == (120.0, 100.0, 1, 20.0)
    assert (usd["paid"], usd["n_paid"]) == (60.0, 2)
    assert (gbp["paid"], gbp["n_paid"]) == (25.0, 1)
