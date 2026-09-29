"""The monthly re-price: every item priced again on the 25th, off the page path.

A background thread (`Scheduler`) wakes every few minutes and asks `due`. A run is
due once each cycle — from the `REPRICE_DAY`th at `REPRICE_HOUR`, local, to the
next — and again, daily, after one that stopped short. A run plans the items not
priced since the cycle began, oldest price first, so a hand refresh after the 25th
saves a search and whatever a stopped run did not reach goes first next time.
An item whose latest price was typed in by hand is left alone: that price stays
put until the owner enters another. A sold price looked up (not typed) is re-priced,
but a newer ask does not outrank it until it is `SOLD_FRESH` old (`current_worth`).

SerpApi's free plan is 250 searches a month and a run of the shelf costs one per
item, so with SerpApi the run reads the account's count first (the Account API is
free) and stops with `REPRICE_RESERVE` searches left for refreshing by hand.

What a run writes: a valuation per item that found listings, marked `via =
'monthly'` — `valuations` stays append-only. An item that found none gets no row,
so it keeps its last price in the total (a hand refresh that finds nothing still
writes its empty row, as before). A request that fails is tried once more, then
skipped; three failures in a row stop the run, as does SerpApi saying the month's
searches are gone. `reprice_runs` / `reprice_items` record all of it for /stats
and the item page, which render from them and never call SerpApi.
"""

import logging
import sqlite3
import threading
import time
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

import httpx

from app import db
from app.pricing import (
    MANUAL_SOLD,
    PricingError,
    PricingSource,
    append_quote,
    current_worth,
    fetch_quote,
)

log = logging.getLogger("steelshelf.reprice")

VIA = "monthly"
RETRY_WAIT = 5.0  # seconds before a failed item is tried once more
FAILS_TO_STOP = 3  # this many failed items in a row and the source is down: stop
RESUME_AFTER = timedelta(days=1)  # a run that stopped short is tried again daily
OUT_OF_SEARCHES = "run out of searches"  # SerpApi's error when the month is spent
ACCOUNT_API = "https://serpapi.com/account.json"

# Why a run stopped short, as stored in reprice_runs.stopped.
RESERVE, QUOTA, ERRORS = "reserve", "quota", "errors"


def _utc(when: datetime) -> str:
    """A local moment as SQLite's `datetime('now')` stamps it (UTC, no zone)."""
    return when.astimezone(UTC).strftime("%Y-%m-%d %H:%M:%S")


def cycle_start(now: datetime, day: int, hour: int) -> datetime:
    """The latest scheduled moment at or before `now`: the `day`th at `hour`, local."""
    day = min(max(day, 1), 28)  # every month has a 28th
    start = now.replace(day=day, hour=hour, minute=0, second=0, microsecond=0)
    if start > now:
        prev = now.replace(day=1) - timedelta(days=1)
        start = prev.replace(day=day, hour=hour, minute=0, second=0, microsecond=0)
    return start


def next_run(now: datetime, day: int, hour: int) -> datetime:
    """The next scheduled moment after `now`."""
    start = cycle_start(now, day, hour)
    return (start.replace(day=1) + timedelta(days=32)).replace(day=start.day)


def last_run(conn: sqlite3.Connection) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM reprice_runs ORDER BY id DESC LIMIT 1").fetchone()


def due(conn: sqlite3.Connection, now: datetime, day: int, hour: int) -> str | None:
    """'schedule' when this cycle has had no run, 'resume' a day after one stopped
    short, else None."""
    last = last_run(conn)
    if last is None or last["started_at"] < _utc(cycle_start(now, day, hour)):
        return "schedule"
    if last["stopped"] and last["started_at"] <= _utc(now - RESUME_AFTER):
        return "resume"
    return None


def planned(conn: sqlite3.Connection, since: str) -> list[int]:
    """Item ids with no price since `since` (UTC), oldest price first, never-priced first.
    Items worth a price typed in by hand (`current_worth`) are left out."""
    vals = db.valuations_by_item(conn)
    rows = []
    for (item_id,) in conn.execute("SELECT id FROM items"):
        mine = vals.get(item_id, [])
        worth = current_worth(mine)
        if worth is not None and worth["source"] == MANUAL_SOLD:
            continue
        newest = mine[0]["fetched_at"] if mine else None
        if newest is None or newest < since:
            rows.append((newest is not None, newest or "", item_id))
    return [item_id for _, _, item_id in sorted(rows)]


def searches_left(api_key: str, http: httpx.Client | None = None) -> int:
    """SerpApi's searches left this month, from the Account API (which spends none)."""
    http = http or httpx.Client(timeout=15)
    r = http.get(ACCOUNT_API, params={"api_key": api_key})
    if r.status_code != 200:
        raise PricingError(f"SerpApi account: HTTP {r.status_code}")
    body = r.json()
    left = body.get("total_searches_left", body.get("plan_searches_left"))
    if left is None:
        raise PricingError("SerpApi account: no search count in the answer")
    return int(left)


def sold_spent(conn: sqlite3.Connection, since: str) -> int:
    """Sold lookups the runs of this cycle have spent."""
    return conn.execute("SELECT coalesce(sum(sold_searches), 0) FROM reprice_runs"
                        " WHERE started_at >= ?", (since,)).fetchone()[0]


def most_valuable_first(conn: sqlite3.Connection, plan: list[int]) -> list[int]:
    """The planned items by what they are worth, highest first, the unpriced last:
    where a sold price moves the total most, and where an ask is least to be trusted."""
    vals = db.valuations_by_item(conn)
    worth = {i: current_worth(vals.get(i, [])) for i in plan}
    return sorted(plan, key=lambda i: -(worth[i]["median"] if worth[i] else -1))


def run(
    conn: sqlite3.Connection,
    source: PricingSource,
    trigger: str,
    since: str,
    account: Callable[[], int] | None = None,
    reserve: int = 20,
    wait: Callable[[float], None] = time.sleep,
    sold: PricingSource | None = None,
    sold_budget: int = 0,
) -> int | None:
    """Re-price the planned items and log the run; its id, or None when nothing was
    run (a resume with no searches to spare yet, or SerpApi's count unreadable).

    With a `sold` source, the most valuable planned items are looked up sold first,
    within what is left of `sold_budget` this cycle; an item whose lookup finds no
    sale, or fails, is priced from asks as usual. `source` is the ask source, and its
    searches are counted against `account`'s count, less `reserve`."""
    plan = planned(conn, since)
    left = None
    if account is not None:
        try:
            left = account()
        except (PricingError, httpx.HTTPError, ValueError) as exc:
            log.warning("re-price not started: %s", exc)
            return None
    allowed = len(plan) if left is None else max(0, left - reserve)
    sold_left = max(0, sold_budget - sold_spent(conn, since)) if sold is not None else 0
    first = set(most_valuable_first(conn, plan)[:sold_left])
    if trigger == "resume" and plan and allowed == 0:
        return None  # still waiting on the month's searches; no row, try tomorrow
    run_id = conn.execute(
        "INSERT INTO reprice_runs (trigger, planned, searches_left) VALUES (?, ?, ?)",
        (trigger, len(plan), left),
    ).lastrowid
    conn.commit()
    counts = {"priced": 0, "no_listings": 0, "failed": 0}
    stopped, fails_in_a_row, reached, spent, sold_used = None, 0, 0, 0, 0
    for n, item_id in enumerate(plan):
        outcome = detail = None
        if item_id in first and sold_used < sold_left:
            outcome, detail, used = _sold(conn, sold, item_id)
            sold_used += used
            if outcome == QUOTA:
                sold_left = 0  # the month's sold lookups are gone; asks carry on
                outcome = None
        if outcome is None:
            if spent >= allowed:
                stopped = RESERVE
                break
            outcome, detail, used = _one(conn, source, item_id, wait)
            if outcome == QUOTA:
                stopped = QUOTA
                break
            spent += used
        reached = n + 1
        counts[outcome] += 1
        fails_in_a_row = fails_in_a_row + 1 if outcome == "failed" else 0
        conn.execute("INSERT INTO reprice_items (run_id, item_id, outcome, detail)"
                     " VALUES (?, ?, ?, ?)", (run_id, item_id, outcome, detail))
        conn.commit()
        if fails_in_a_row >= FAILS_TO_STOP:
            stopped = ERRORS
            break
    conn.executemany(
        "INSERT INTO reprice_items (run_id, item_id, outcome) VALUES (?, ?, 'not_reached')",
        [(run_id, item_id) for item_id in plan[reached:]],
    )
    conn.execute(
        "UPDATE reprice_runs SET finished_at = datetime('now'), priced = ?, no_listings = ?,"
        " failed = ?, stopped = ?, sold_searches = ? WHERE id = ?",
        (counts["priced"], counts["no_listings"], counts["failed"], stopped, sold_used, run_id),
    )
    conn.commit()
    log.info("re-price run %d (%s): %d of %d priced, %d no listings, %d failed, "
             "%d sold lookups, stopped %s", run_id, trigger, counts["priced"], len(plan),
             counts["no_listings"], counts["failed"], sold_used, stopped)
    return run_id


def _sold(conn, source, item_id) -> tuple[str | None, str | None, int]:
    """('priced', 'sold', searches) when a sold lookup priced the item; (None, why,
    searches) when it found no sale or failed, so asks price it; QUOTA when the
    vendor says the month's requests are spent. Tried once: a quota is small."""
    try:
        quote = fetch_quote(conn, item_id, source)
    except PricingError as exc:
        text = str(exc).lower()
        if "quota" in text or "http 429" in text or OUT_OF_SEARCHES in text:
            log.warning("sold lookups stopped for this run: %s", exc)
            return QUOTA, None, 1
        log.info("sold lookup of item %d failed, asks instead: %s", item_id, exc)
        return None, None, 1
    if not quote.n_listings:
        return None, None, quote.searches
    append_quote(conn, item_id, quote, VIA)
    return "priced", "sold", quote.searches


def _one(conn, source, item_id, wait) -> tuple[str, str | None, int]:
    """('priced' | 'no_listings' | 'failed' | QUOTA, detail, searches spent) for one
    item, tried twice."""
    try:
        try:
            quote = fetch_quote(conn, item_id, source)
        except PricingError as exc:
            if OUT_OF_SEARCHES in str(exc).lower():
                raise
            wait(RETRY_WAIT)
            quote = fetch_quote(conn, item_id, source)
    except PricingError as exc:
        if OUT_OF_SEARCHES in str(exc).lower():
            return QUOTA, str(exc), 0
        return "failed", str(exc)[:200], 1
    if not quote.n_listings:
        return "no_listings", None, quote.searches
    append_quote(conn, item_id, quote, VIA)
    return "priced", None, quote.searches


# --- what the pages show ----------------------------------------------------------


def missed(conn: sqlite3.Connection, run: sqlite3.Row | None) -> list[sqlite3.Row]:
    """The items a run did not re-price and nothing has priced since: its not reached,
    failed and no-listings ones, with their outcome, by title."""
    if run is None:
        return []
    return conn.execute(
        """
        SELECT i.id, i.title, r.outcome, r.detail FROM reprice_items r
        JOIN items i ON i.id = r.item_id
        WHERE r.run_id = ? AND r.outcome != 'priced'
          AND NOT EXISTS (SELECT 1 FROM valuations v WHERE v.item_id = i.id
                          AND v.fetched_at >= ?)
        ORDER BY i.title COLLATE NOCASE
        """,
        (run["id"], run["started_at"]),
    ).fetchall()


def item_note(conn: sqlite3.Connection, item_id: int) -> sqlite3.Row | None:
    """The latest run's outcome for this item when it was missed and not priced since."""
    run = last_run(conn)
    return next((r for r in missed(conn, run) if r["id"] == item_id), None)


def run_days(conn: sqlite3.Connection) -> dict[str, str]:
    """Each run's started_at, by run id order: {stamp: 'run' | 'partial'} for the chart."""
    return {r["started_at"]: "partial" if r["stopped"] else "run"
            for r in conn.execute("SELECT started_at, stopped FROM reprice_runs ORDER BY id")}


class Scheduler:
    """The thread that starts runs when they are due, and one started by hand.

    `job` returns (source, account[, (sold source, sold budget)]) — the ask source,
    SerpApi's count reader (None for a source with no quota), and the sold source to
    try first with its per-cycle budget — or None when no source is set up, in which
    case nothing runs. One run at a time.
    """

    CHECK_EVERY = 600.0  # seconds between looks at the clock
    FIRST_LOOK = 60.0  # after startup, so a restart is not held up

    def __init__(self, database_path: str, job, day: int, hour: int, reserve: int,
                 now: Callable[[], datetime] = lambda: datetime.now().astimezone()):
        self.database_path = database_path
        self.job, self.day, self.hour, self.reserve, self.now = job, day, hour, reserve, now
        self.lock = threading.Lock()
        self.stop = threading.Event()
        self.thread: threading.Thread | None = None

    @property
    def running(self) -> bool:
        return self.lock.locked()

    def start(self) -> None:
        self.thread = threading.Thread(target=self._loop, name="reprice", daemon=True)
        self.thread.start()

    def _loop(self) -> None:
        wait = self.FIRST_LOOK
        while not self.stop.wait(wait):
            wait = self.CHECK_EVERY
            try:
                self.tick()
            except Exception:
                log.exception("re-price check failed")

    def tick(self, trigger: str | None = None) -> int | None:
        """Run now if due (or if `trigger` is given, by hand); the run's id or None."""
        job = self.job()
        if job is None or not self.lock.acquire(blocking=False):
            return None
        try:
            with db.connect(self.database_path) as conn:
                now = self.now()
                trigger = trigger or due(conn, now, self.day, self.hour)
                if trigger is None:
                    return None
                source, account, *more = job
                sold, budget = more[0] if more and more[0] else (None, 0)
                since = _utc(cycle_start(now, self.day, self.hour))
                return run(conn, source, trigger, since, account, self.reserve,
                           sold=sold, sold_budget=budget)
        finally:
            self.lock.release()

    def start_by_hand(self) -> bool:
        """Run the rest now, in the background; False when one is already going."""
        if self.running or self.job() is None:
            return False
        threading.Thread(target=self.tick, args=("hand",), name="reprice-hand",
                         daemon=True).start()
        return True
