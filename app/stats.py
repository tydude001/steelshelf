"""The shelf in numbers: what the /stats page counts, from `db.list_items` rows.

Pure functions; nothing here touches the database or the network. Each bar
carries its share of the largest bar (`pct`), which is what the page draws.
"""

import sqlite3
import statistics
from dataclasses import dataclass, field

from app import shelf
from app.pricing import FILM_WIDE, SOLD_SOURCES, confidence


@dataclass(frozen=True)
class Bar:
    name: str
    value: float | None  # None: nothing in it has a price, shown as a dash
    pct: float  # of the largest bar in its list, 0–100


def _bars(pairs: list[tuple[str, float | None]]) -> list[Bar]:
    top = max((v for _, v in pairs if v is not None), default=0)
    return [Bar(name, value, round(value / top * 100, 1) if top and value else 0.0)
            for name, value in pairs]


def value_by_label(items: list[sqlite3.Row]) -> list[Bar]:
    """Each retailer or label's total, largest first, the unnamed group among them.

    A group with no priced item has no total (None), not zero, and goes last.
    """
    totals = [(g.name, round(g.total, 2) if any(it["latest_median"] is not None
                                                 for it in g.items) else None)
              for g in shelf.shelve(items, "label")]
    return _bars(sorted(totals, key=lambda p: (p[1] is None, -(p[1] or 0))))


@dataclass
class Certainty:
    """How many items' latest price is solid, fair or thin, and who the thin ones are."""

    solid: int = 0
    fair: int = 0
    thin: int = 0
    unpriced: int = 0
    sold: int = 0  # priced from sold listings, not asks
    thin_items: list[sqlite3.Row] = field(default_factory=list)

    @property
    def priced(self) -> int:
        return self.solid + self.fair + self.thin

    def pct(self, n: int) -> float:
        return round(n / self.priced * 100, 1) if self.priced else 0.0


def _get(it, key: str):
    """A column that rows from before it existed may lack."""
    return it[key] if key in it.keys() else None


def film_wide(items: list[sqlite3.Row]) -> list[sqlite3.Row]:
    """Items whose price was counted across every steelbook of their film, by title:
    no listing named the edition, so it is the film's price, not this case's."""
    return sorted((it for it in items if _get(it, "latest_matched") == FILM_WIDE),
                  key=lambda it: shelf.sort_title(it["title"]))


def certainty(items: list[sqlite3.Row]) -> Certainty:
    c = Certainty()
    for it in items:
        cue = confidence(it["latest_n"], it["latest_low"], it["latest_high"],
                         it["latest_median"], matched=_get(it, "latest_matched"))
        if cue is None:
            c.unpriced += 1
            continue
        if it["latest_source"] in SOLD_SOURCES:
            c.sold += 1
        if cue.level == 3:
            c.solid += 1
        elif cue.level == 2:
            c.fair += 1
        else:
            c.thin += 1
            c.thin_items.append(it)
    c.thin_items.sort(key=lambda it: shelf.sort_title(it["title"]))
    return c


# How many items need both a sold price and an ask before their ratio says much.
CALIBRATE_FROM = 20


@dataclass(frozen=True)
class AskVsSold:
    """Items with both a sold price and an ask: how far apart the two run."""

    n: int
    ratio: float | None  # the median of sold ÷ ask, once there are CALIBRATE_FROM
    pairs: list[tuple[int, float, float]]  # (item id, sold median, ask median)


def ask_vs_sold(valuations: dict[int, list]) -> AskVsSold:
    """Each item's newest sold price against its newest ask, from its valuations (newest
    first). The ratio waits for `CALIBRATE_FROM` items: until then the shelf shows the
    two side by side rather than correct asks by a guess."""
    pairs = []
    for item_id, vals in valuations.items():
        priced = [v for v in vals if v["median"]]
        sold = next((v for v in priced if v["source"] in SOLD_SOURCES), None)
        ask = next((v for v in priced if v["source"] not in SOLD_SOURCES), None)
        if sold and ask:
            pairs.append((item_id, sold["median"], ask["median"]))
    ratio = (round(statistics.median(s / a for _, s, a in pairs), 2)
             if len(pairs) >= CALIBRATE_FROM else None)
    return AskVsSold(len(pairs), ratio, pairs)


def _format(fmt: str | None) -> str:
    f = (fmt or "").lower()
    return "4K" if "4k" in f else "Blu-ray" if "blu" in f else "DVD" if "dvd" in f else (
        "other" if f else "not given")


def _condition(cond: str | None) -> str:
    c = (cond or "").strip().lower()
    return "sealed" if "sealed" in c else "opened" if c else "not given"


def _count(items, key, order=None, keep: int | None = None) -> list[Bar]:
    counts: dict[str, int] = {}
    for it in items:
        name = key(it)
        counts[name] = counts.get(name, 0) + 1
    tail = {"other", "not given"}
    ranked = sorted(counts.items(), key=lambda p: (
        p[0] in tail, order.index(p[0]) if order and p[0] in order else 0, -p[1], p[0]))
    if keep is not None:  # the biggest `keep` by name, the rest pooled into "other"
        named = [p for p in ranked if p[0] not in tail]
        rest = sum(n for _, n in named[keep:]) + counts.get("other", 0)
        ranked = named[:keep] + ([("other", rest)] if rest else []) + (
            [("not given", counts["not given"])] if "not given" in counts else [])
    return _bars(ranked)


def breakdowns(items: list[sqlite3.Row]) -> dict[str, list[Bar]]:
    """Counts by format, by region (the three biggest, the rest pooled) and by condition."""
    return {
        "Format": _count(items, lambda it: _format(it["format"]),
                         order=["4K", "Blu-ray", "DVD"]),
        "Region": _count(items, lambda it: (it["region"] or "").strip() or "not given",
                         keep=3),
        "Condition": _count(items, lambda it: _condition(it["condition"]),
                            order=["opened", "sealed"]),
    }


def as_of(items: list[sqlite3.Row]) -> str | None:
    """The newest latest-price moment on the shelf: what the numbers are as of."""
    return max((it["latest_fetched_at"] for it in items if it["latest_fetched_at"]),
               default=None)
