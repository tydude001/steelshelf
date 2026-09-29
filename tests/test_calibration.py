"""The value chart's paid line, and sold prices set against asks on /stats."""

from app import present, shelf, stats

from tests.conftest import AUTH
from tests.test_items import post_item, rows  # noqa: F401


def v(item_id, median, at, source="serpapi_active"):
    return {"item_id": item_id, "median": median, "currency": "USD", "fetched_at": at,
            "source": source}


def test_paid_follows_the_items_the_value_counts():
    history = [v(1, 50.0, "2026-07-01 12:00:00"), v(2, 30.0, "2026-08-01 12:00:00"),
               v(1, 80.0, "2026-09-01 12:00:00")]
    paid = {1: 40.0, 2: 25.0, 3: 99.0}  # 3 never priced: never in the total
    assert [p for _, p in shelf.paid_over_time(history, "USD", paid)] == [40.0, 65.0, 65.0]
    assert [t for _, t in shelf.value_over_time(history, "USD")] == [50.0, 80.0, 110.0]


def test_the_paid_line_shares_the_values_scale():
    c = present.line([("2026-07-01 12:00:00", 100.0), ("2026-08-01 12:00:00", 200.0)],
                     beside=[50.0, 60.0])
    assert [p.value for p in c.beside] == [50.0, 60.0]
    # 50 is the lowest of both series, so it sits on the floor; 200 at the top.
    assert c.beside[0].y == c.height - 24 and c.points[1].y == 30
    assert c.beside_polyline.count(",") == 2


def test_ask_vs_sold_waits_for_twenty_items():
    few = {1: [v(1, 60.0, "2", "soldcomps_sold"), v(1, 50.0, "1")]}
    k = stats.ask_vs_sold(few)
    assert (k.n, k.ratio, k.pairs) == (1, None, [(1, 60.0, 50.0)])
    many = {i: [v(i, 45.0, "2", "manual_sold"), v(i, 50.0, "1")] for i in range(20)}
    many[99] = [v(99, 10.0, "1")]  # asks only: not a pair
    k = stats.ask_vs_sold(many)
    assert (k.n, k.ratio) == (20, 0.9)


def test_stats_sets_sold_beside_asks(admin):
    loc = post_item(admin, data={"title": "Alien"}).headers["location"]
    admin.post(f"{loc}/sold", data={"prices": "40, 60"}, auth=AUTH)
    page = admin.get("/stats").text
    assert "Sold prices and asks" in page and "<b>50 USD</b> of the value is\n      1 item" in page
    assert "0\n      items have both" in page and "at 20 the shelf can say" in page
