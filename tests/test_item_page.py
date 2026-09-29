"""The item page's value card: the confidence cue, the history line, dates."""

import time

from app import present
from app.pricing import confidence

from tests.conftest import AUTH
from tests.test_items import StubSource, post_item, stub_pricing  # noqa: F401


# --- confidence ------------------------------------------------------------------


def test_four_or_fewer_is_thin_whatever_the_spread():
    c = confidence(4, 30, 31, 30.5)
    assert (c.level, c.word) == (1, "Thin")
    assert c.sentence.startswith("Only 4 listings")
    assert confidence(1, 30, 30, 30, "sale").sentence.startswith("Only 1 sale;")


def test_many_close_together_is_solid_and_a_wide_range_caps_it_at_fair():
    assert confidence(12, 30, 40, 35)[:2] == (3, "Solid")
    wide = confidence(20, 20, 110, 35)
    assert wide[:2] == (2, "Fair") and "20 to 110" in wide.sentence


def test_a_middling_count_is_fair_and_nothing_priced_has_no_cue():
    assert confidence(7, 30, 40, 35)[:2] == (2, "Fair")
    assert confidence(0, None, None, None) is None
    assert confidence(3, None, None, None) is None


# --- dates and chart geometry -----------------------------------------------------


def test_day_is_the_date_only_in_the_local_zone(monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    assert present.day("2026-09-25 14:03:11") == "25 Sep 2026"
    assert present.short_day("2026-06-05 00:00:00") == "5 Jun"
    assert present.day(None) == ""
    # SQLite's stamps are UTC: an evening in New York is still the day before.
    monkeypatch.setenv("TZ", "America/New_York")
    time.tzset()
    assert present.day("2026-09-26 02:00:00") == "25 Sep 2026"
    monkeypatch.delenv("TZ")
    time.tzset()


def test_line_puts_time_on_x_and_value_on_y():
    c = present.line([("2026-06-01 00:00:00", 10.0), ("2026-06-02 00:00:00", 30.0),
                      ("2026-06-05 00:00:00", 20.0)], width=100, height=60,
                     pad_x=10, top=10, bottom=10)
    xs, ys = [p.x for p in c.points], [p.y for p in c.points]
    assert xs == [10, 30, 90]  # one day of four across the 80 between the pads
    assert ys == [50, 10, 30]  # lowest on the floor, highest at the top
    assert c.polyline == "10,50 30,10 90,30"
    assert (c.first.value, c.last.value) == (10.0, 20.0)


def test_line_of_one_or_of_one_moment_or_flat():
    (only,) = present.line([("2026-06-01 00:00:00", 5.0)], width=100, height=60,
                           pad_x=10, top=10, bottom=10).points
    assert (only.x, only.y) == (50, 30)
    same = present.line([("2026-06-01 00:00:00", 5.0)] * 3, width=100, height=60,
                        pad_x=10, top=10, bottom=10)
    assert [p.x for p in same.points] == [10, 50, 90]
    assert present.line([]) is None


# --- the page -----------------------------------------------------------------------


def test_value_card_says_worth_about_with_one_primary_button(admin, stub_pricing):  # noqa: F811
    stub_pricing(StubSource())  # 7 listings, 20 to 80 around 35: a wide spread
    loc = post_item(admin).headers["location"]
    page = admin.get(loc).text
    assert "Not priced yet." in page and "History" not in page
    admin.post(f"{loc}/value", auth=AUTH)
    page = admin.get(loc).text
    assert "Worth about" in page and "35.00 USD" in page
    assert "Fair confidence." in page and "spread wide from 20 to 80" in page
    assert page.count('class="btn wide"') == 1 and "Refresh price" in page
    assert '<details class="sold-entry">' in page and '<details class="how small">' in page
    assert f'href="{loc}/edit">Edit</a>' in page and "Shelf</a>" in page


def test_history_is_a_dot_then_a_line(admin, stub_pricing):  # noqa: F811
    stub_pricing(StubSource())
    loc = post_item(admin).headers["location"]
    admin.post(f"{loc}/value", auth=AUTH)
    page = admin.get(loc).text
    assert page.count('class="dot') == 1 and "<polyline" not in page
    assert "a line once there is a second fetch" in page
    admin.post(f"{loc}/value", auth=AUTH)
    page = admin.get(loc).text
    assert page.count('class="dot') == 2 and '<polyline class="trend"' in page
    assert "a line once there is a second fetch" not in page
    assert "Every fetch (2)" in page
