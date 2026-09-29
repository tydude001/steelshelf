"""The monthly re-price: when it is due, what it prices, and how it stops."""

import time
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from app import db, reprice
from app import settings as settings_module
from app.pricing import Listing, PricingError, from_listings, summarize

from tests.conftest import AUTH

CHICAGO = timezone(timedelta(hours=-5))


def at(y, m, d, h=12):
    return datetime(y, m, d, h, tzinfo=CHICAGO)


class Fake:
    """A pricing source that answers from a script: title → Quote, exception, or list."""

    name = "fake"

    def __init__(self, answers=None):
        self.answers = answers or {}
        self.asked = []

    def quote(self, item):
        self.asked.append(item["title"])
        a = self.answers.get(item["title"], 30.0)
        if isinstance(a, list):
            a = a.pop(0)
        if isinstance(a, Exception):
            raise a
        if a is None:
            return summarize("fake", [], "USD")
        return from_listings("fake", [Listing(item["title"], a, key=item["title"])], "USD")


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "r.db")
    db.init_db(path)
    c = db.connect(path)
    yield c
    c.close()


def add(conn, title, priced_at=None, source="fake", median=20.0):
    item_id = db.insert_item(conn, {"title": title})
    if priced_at:
        conn.execute("INSERT INTO valuations (item_id, source, median, n_listings, fetched_at)"
                     " VALUES (?, ?, ?, 1, ?)", (item_id, source, median, priced_at))
    conn.commit()
    return item_id


SINCE = "2026-09-25 08:00:00"  # 25 Sep 03:00 Chicago-ish, as UTC


def outcomes(conn, run_id):
    return {r["title"]: r["outcome"] for r in conn.execute(
        "SELECT i.title, r.outcome FROM reprice_items r JOIN items i ON i.id = r.item_id"
        " WHERE r.run_id = ?", (run_id,))}


# --- the calendar -------------------------------------------------------------------


def test_cycle_starts_on_the_25th_at_three():
    assert reprice.cycle_start(at(2026, 9, 25, 2), 25, 3) == at(2026, 8, 25, 3)
    assert reprice.cycle_start(at(2026, 9, 25, 4), 25, 3) == at(2026, 9, 25, 3)
    assert reprice.cycle_start(at(2027, 1, 3), 25, 3) == at(2026, 12, 25, 3)
    assert reprice.next_run(at(2026, 9, 26), 25, 3) == at(2026, 10, 25, 3)
    assert reprice.next_run(at(2026, 12, 30), 25, 3) == at(2027, 1, 25, 3)


def test_due_once_a_cycle_and_daily_after_a_stop(conn):
    now = at(2026, 9, 26)
    assert reprice.due(conn, now, 25, 3) == "schedule"
    conn.execute("INSERT INTO reprice_runs (trigger, planned, started_at) VALUES"
                 " ('schedule', 0, '2026-09-25 09:00:00')")
    assert reprice.due(conn, now, 25, 3) is None
    conn.execute("UPDATE reprice_runs SET stopped = 'reserve'")
    assert reprice.due(conn, at(2026, 9, 25, 20), 25, 3) is None  # not a day yet
    assert reprice.due(conn, at(2026, 9, 27), 25, 3) == "resume"
    assert reprice.due(conn, at(2026, 10, 25, 4), 25, 3) == "schedule"


# --- what a run prices ----------------------------------------------------------------


def test_plan_is_oldest_price_first_and_skips_the_fresh_and_the_typed_in(conn):
    add(conn, "Fresh", "2026-09-25 12:00:00")
    add(conn, "Old", "2026-08-01 00:00:00")
    add(conn, "Older", "2026-07-01 00:00:00")
    add(conn, "Never")
    add(conn, "Typed", "2026-07-01 00:00:00", source="manual_sold")
    titles = {r["id"]: r["title"] for r in conn.execute("SELECT id, title FROM items")}
    assert [titles[i] for i in reprice.planned(conn, SINCE)] == ["Never", "Older", "Old"]


def test_a_run_appends_monthly_rows_and_writes_nothing_for_no_listings(conn):
    a = add(conn, "Alien", "2026-08-01 00:00:00")
    b = add(conn, "Brazil", "2026-08-01 00:00:00", median=55.0)
    run_id = reprice.run(conn, Fake({"Brazil": None}), "schedule", SINCE, wait=lambda s: None)
    assert outcomes(conn, run_id) == {"Alien": "priced", "Brazil": "no_listings"}
    rows = conn.execute("SELECT item_id, via, median FROM valuations ORDER BY id").fetchall()
    assert [(r["item_id"], r["via"]) for r in rows[-1:]] == [(a, "monthly")]
    assert len([r for r in rows if r["item_id"] == b]) == 1  # Brazil keeps its 55
    run = reprice.last_run(conn)
    assert (run["planned"], run["priced"], run["no_listings"], run["stopped"]) == (2, 1, 1, None)
    assert run["finished_at"]


def test_a_failure_is_tried_once_more_then_skipped(conn):
    add(conn, "Alien")
    add(conn, "Brazil")
    waits = []
    fake = Fake({"Alien": [PricingError("timeout"), 30.0],
                 "Brazil": [PricingError("boom"), PricingError("boom")]})
    run_id = reprice.run(conn, fake, "schedule", SINCE, wait=waits.append)
    assert outcomes(conn, run_id) == {"Alien": "priced", "Brazil": "failed"}
    assert waits == [reprice.RETRY_WAIT, reprice.RETRY_WAIT]


def test_three_failures_in_a_row_stop_the_run(conn):
    for t in "ABCDE":
        add(conn, t)
    fake = Fake({t: PricingError("down") for t in "ABCDE"})
    run_id = reprice.run(conn, fake, "schedule", SINCE, wait=lambda s: None)
    assert reprice.last_run(conn)["stopped"] == reprice.ERRORS
    assert sorted(outcomes(conn, run_id).values()) == ["failed"] * 3 + ["not_reached"] * 2


def test_running_out_of_searches_stops_and_leaves_the_rest(conn):
    for t in "ABC":
        add(conn, t)
    fake = Fake({"B": PricingError("SerpApi search failed: Your account has run out of searches.")})
    run_id = reprice.run(conn, fake, "schedule", SINCE, wait=lambda s: None)
    assert outcomes(conn, run_id) == {"A": "priced", "B": "not_reached", "C": "not_reached"}
    assert reprice.last_run(conn)["stopped"] == reprice.QUOTA
    assert fake.asked == ["A", "B"]  # B not tried twice


def test_the_reserve_is_kept_back(conn):
    for t in "ABCD":
        add(conn, t)
    fake = Fake()
    run_id = reprice.run(conn, fake, "schedule", SINCE, account=lambda: 22, reserve=20)
    assert fake.asked == ["A", "B"]
    run = reprice.last_run(conn)
    assert (run["stopped"], run["searches_left"], run["priced"]) == ("reserve", 22, 2)
    assert list(outcomes(conn, run_id).values()).count("not_reached") == 2


def test_a_resume_with_nothing_to_spare_writes_no_row(conn):
    add(conn, "A")
    assert reprice.run(conn, Fake(), "resume", SINCE, account=lambda: 15, reserve=20) is None
    assert reprice.last_run(conn) is None


def test_an_unreadable_count_starts_nothing(conn):
    add(conn, "A")

    def broken():
        raise PricingError("SerpApi account: HTTP 500")

    assert reprice.run(conn, Fake(), "schedule", SINCE, account=broken) is None


def test_missed_items_until_something_prices_them(conn):
    a = add(conn, "A")
    add(conn, "B")
    reprice.run(conn, Fake(), "schedule", SINCE, account=lambda: 21, reserve=20)
    assert [m["title"] for m in reprice.missed(conn, reprice.last_run(conn))] == ["B"]
    assert reprice.item_note(conn, a) is None
    b = conn.execute("SELECT id FROM items WHERE title = 'B'").fetchone()[0]
    assert reprice.item_note(conn, b)["outcome"] == "not_reached"
    conn.execute("INSERT INTO valuations (item_id, source, median, n_listings, fetched_at)"
                 " VALUES (?, 'fake', 9, 1, '2999-01-01 00:00:00')", (b,))
    assert reprice.item_note(conn, b) is None


# --- the scheduler ------------------------------------------------------------------------


def test_scheduler_runs_when_due_and_not_again(conn, tmp_path):
    add(conn, "A")
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    fake = Fake()
    s = reprice.Scheduler(path, lambda: (fake, None), 25, 3, 20, now=lambda: at(2026, 9, 26))
    assert s.tick() is not None and fake.asked == ["A"]
    assert s.tick() is None  # this cycle is done
    off = reprice.Scheduler(path, lambda: None, 25, 3, 20, now=lambda: at(2026, 9, 26))
    assert off.tick("hand") is None


# --- the pages ------------------------------------------------------------------------------


def test_stats_says_off_with_no_source(client):
    page = client.get("/stats").text
    assert 'id="reprice"' in page and "No pricing" in page


def test_stats_and_item_page_show_a_run_that_stopped(admin, monkeypatch):
    from app import main

    monkeypatch.setattr(main, "reprice_job", lambda: (Fake(), None))
    ids = []
    for t in ("Alien", "Brazil"):
        r = admin.post("/items", data={"title": t}, auth=AUTH, follow_redirects=False,
                       files={"front": ("f.jpg", b"\xff\xd8x", "image/jpeg")})
        ids.append(int(r.headers["location"].rsplit("/", 1)[1]))
    with db.connect(settings_module.settings.database_path) as conn:
        reprice.run(conn, Fake(), "schedule", SINCE, account=lambda: 21, reserve=20)
    stats = admin.get("/stats").text
    assert "stopped at 1 of 2" in stats and "kept" in stats and "Run the rest now" in stats
    item = admin.get(f"/items/{ids[1]}").text
    assert "didn’t reach this one" in item
    assert "monthly" in admin.get(f"/items/{ids[0]}").text
    assert "Re-priced monthly" in admin.get("/").text


def test_run_the_rest_needs_auth_and_starts_a_run(admin, monkeypatch):
    from app import main

    fake = Fake()
    monkeypatch.setattr(main, "reprice_job", lambda: (fake, None))
    admin.post("/items", data={"title": "Alien"}, auth=AUTH,
               files={"front": ("f.jpg", b"\xff\xd8x", "image/jpeg")})
    assert admin.post("/reprice", follow_redirects=False).status_code == 401
    r = admin.post("/reprice", auth=AUTH, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/stats#reprice"
    for _ in range(50):
        if fake.asked:
            break
        time.sleep(0.05)
    assert fake.asked == ["Alien"]


# --- sold first -------------------------------------------------------------------------


class FakeSold(Fake):
    name = "soldcomps_sold"

    def quote(self, item):
        q = super().quote(item)
        return replace(q, source="soldcomps_sold")


def test_sold_first_for_the_most_valuable_within_the_budget(conn):
    cheap = add(conn, "Cheap", "2026-08-01 00:00:00", median=10.0)
    dear = add(conn, "Dear", "2026-08-01 00:00:00", median=600.0)
    mid = add(conn, "Mid", "2026-08-01 00:00:00", median=50.0)
    sold, asks = FakeSold({"Dear": 630.0, "Mid": None}), Fake()
    run_id = reprice.run(conn, asks, "schedule", SINCE, wait=lambda s: None,
                         sold=sold, sold_budget=2)
    assert sorted(sold.asked) == ["Dear", "Mid"]  # the two most valuable
    assert sorted(asks.asked) == ["Cheap", "Mid"]  # Mid found no sale: asks price it
    newest = {r["item_id"]: r["source"] for r in conn.execute(
        "SELECT item_id, source FROM valuations WHERE via = 'monthly'")}
    assert newest == {dear: "soldcomps_sold", mid: "fake", cheap: "fake"}
    assert reprice.last_run(conn)["sold_searches"] == 2
    assert outcomes(conn, run_id) == {"Cheap": "priced", "Dear": "priced", "Mid": "priced"}
    assert reprice.sold_spent(conn, SINCE) == 2


def test_the_sold_budget_spans_the_cycle_and_a_quota_answer_stops_it(conn):
    for t in "ABC":
        add(conn, t, "2026-08-01 00:00:00")
    conn.execute("INSERT INTO reprice_runs (trigger, planned, started_at, sold_searches)"
                 " VALUES ('hand', 0, '2026-09-26 00:00:00', 79)")
    sold = FakeSold()
    reprice.run(conn, Fake(), "hand", SINCE, wait=lambda s: None, sold=sold, sold_budget=80)
    assert len(sold.asked) == 1  # one left of 80
    for t in "DEF":
        add(conn, t, "2026-08-01 00:00:00")
    quota = FakeSold({t: PricingError("SoldComps search failed: HTTP 429 quota_exceeded")
                      for t in "DEF"})
    asks = Fake()
    reprice.run(conn, asks, "hand", "2026-09-26 00:00:01", wait=lambda s: None,
                sold=quota, sold_budget=80)
    assert len(quota.asked) == 1 and sorted(asks.asked) == ["D", "E", "F"]


def test_the_scheduler_passes_the_sold_source_on(conn):
    add(conn, "A")
    path = conn.execute("PRAGMA database_list").fetchone()["file"]
    sold = FakeSold()
    s = reprice.Scheduler(path, lambda: (Fake(), None, (sold, 5)), 25, 3, 20,
                          now=lambda: at(2026, 9, 26))
    assert s.tick() is not None and sold.asked == ["A"]
