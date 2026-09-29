"""How the shelf is ordered: one sort key in, shelves of items out.

`SORTS` names every order the index offers. Five of them group the shelf —
by retailer or label, format, region, genre, director — and each group gets its
own run of shelf with a plaque; the rest order one continuous shelf. The choice rides on
`?sort=` and is remembered in a cookie, so a phone that picked "value" once
comes back to it.

`most_valuable` and `value_over_time` feed the side column beside the bookcase.
`spine_face` says which spine a case shows on the shelf, and `spine_check` why
one needs the owner's eye on /review.

Pure functions over the rows `db.list_items` (and `db.valuation_history`)
return; nothing here touches the database or the network.
"""

import re
import sqlite3
from dataclasses import dataclass, field

from app import present
from app.pricing import current_worth
from app.spine import SLIP_RATIO

# key → (label on the chip, groups the shelf?)
SORTS = {
    "newest": ("Newest", False),
    "title": ("Title", False),
    "value": ("Value", False),
    "label": ("By label", True),
    "format": ("By format", True),
    "region": ("By region", True),
    "genre": ("By genre", True),
    "director": ("By director", True),
}
DEFAULT_SORT = "newest"

_ARTICLE = re.compile(r"^(the|a|an)\s+", re.IGNORECASE)
NO_LABEL, NO_FORMAT, NO_REGION = "No retailer named", "No format", "No region"
NO_GENRE, NO_DIRECTOR = "No genre", "No director"


@dataclass
class Shelf:
    """One run of shelf: a plaque (None on an ungrouped shelf) and its items."""

    name: str | None
    items: list[sqlite3.Row] = field(default_factory=list)

    @property
    def total(self) -> float:
        return sum(it["latest_median"] or 0 for it in self.items)

    @property
    def plaque(self) -> str:
        n = len(self.items)
        text = f"{self.name} · {n}"
        priced = [it for it in self.items if it["latest_median"] is not None]
        if priced:
            text += f" · {self.total:,.0f} {priced[0]['latest_currency']}"
        return text


def sort_title(title: str) -> str:
    """'The Batman' files under B, like a shop shelf."""
    return _ARTICLE.sub("", (title or "").strip()).casefold()


def _format_rank(fmt: str | None) -> tuple:
    f = (fmt or "").lower()
    return (0 if "4k" in f else 1 if "blu" in f else 2 if "dvd" in f else 3, f)


def _newest(items):
    return sorted(items, key=lambda it: (it["created_at"], it["id"]), reverse=True)


def _by_title(items):
    return sorted(items, key=lambda it: (sort_title(it["title"]), it["id"]))


def _by_value(items):
    # Priced items highest first; the unpriced trail, newest first among themselves.
    return sorted(
        items,
        key=lambda it: (it["latest_median"] is None, -(it["latest_median"] or 0),
                        it["created_at"]),
    )


def _grouped(items, key: str, blank: str, order):
    groups: dict[str, Shelf] = {}
    for it in _by_title(items):
        name = (it[key] or "").strip() or blank
        groups.setdefault(name, Shelf(name)).items.append(it)
    # The blank group goes last whatever its size.
    return sorted(groups.values(), key=lambda s: (s.name == blank, order(s)))


def shelve(items: list[sqlite3.Row], sort: str) -> list[Shelf]:
    """The shelf runs for a sort key; an unknown key is the default."""
    if sort not in SORTS:
        sort = DEFAULT_SORT
    if sort == "title":
        return [Shelf(None, _by_title(items))]
    if sort == "value":
        return [Shelf(None, _by_value(items))]
    if sort == "label":
        return _grouped(items, "retailer", NO_LABEL, lambda s: (-len(s.items), s.name))
    if sort == "format":
        return _grouped(items, "format", NO_FORMAT, lambda s: _format_rank(s.name))
    if sort == "region":
        return _grouped(items, "region", NO_REGION, lambda s: (-len(s.items), s.name))
    if sort == "genre":
        return _grouped(items, "genre", NO_GENRE, lambda s: (-len(s.items), s.name))
    if sort == "director":
        return _grouped(items, "director", NO_DIRECTOR, lambda s: (-len(s.items), s.name))
    return [Shelf(None, _newest(items))]


SEARCHED = ("title", "edition", "retailer", "region", "format", "notes", "genre", "director")


def search(items: list[sqlite3.Row], q: str | None) -> list[sqlite3.Row]:
    """The items matching every word of `q` in any searched field, ignoring case.

    A blank query matches everything.
    """
    words = (q or "").casefold().split()
    if not words:
        return list(items)
    found = []
    for it in items:
        text = " ".join(it[f] or "" for f in SEARCHED).casefold()
        if all(w in text for w in words):
            found.append(it)
    return found


def most_valuable(items: list[sqlite3.Row], n: int = 5) -> list[sqlite3.Row]:
    """The `n` items with the highest latest median; the unpriced never make it."""
    priced = [it for it in items if it["latest_median"] is not None]
    return sorted(priced, key=lambda it: (-it["latest_median"], sort_title(it["title"])))[:n]


def value_over_time(history: list[sqlite3.Row], currency: str) -> list[tuple[str, float]]:
    """The shelf's total after each day that had a fetch, as (last stamp that day, total).

    Walks the valuations oldest first and, after each, sums every item's
    `pricing.current_worth` as it stood at that moment — the rule the shelf total
    uses — so the last point is today's total: a fetch that found nothing takes its
    item out, and a sold price holds against later asks until it ages out. Days are
    local.
    """
    return [(stamp, round(sum(worth.values()), 2)) for stamp, worth in _walk(history, currency)]


def paid_over_time(history: list[sqlite3.Row], currency: str,
                   paid: dict[int, float]) -> list[tuple[str, float]]:
    """What was paid for the items counted in `value_over_time`'s total, at each of its
    points: set beside the value, a case bought lifts both lines, a price that rose
    lifts only the value. `paid` is item id → paid price, for the items that have one."""
    return [(stamp, round(sum(paid.get(i, 0.0) for i in worth), 2))
            for stamp, worth in _walk(history, currency)]


def _walk(history, currency: str) -> list[tuple[str, dict[int, float]]]:
    """(last stamp that day, {item: what it was worth then}) after each day with a fetch."""
    seen: dict[int, list] = {}  # item → its valuations so far, newest first
    days: dict[str, tuple[str, dict[int, float]]] = {}
    for v in history:
        if v["currency"] != currency:
            continue
        seen.setdefault(v["item_id"], []).insert(0, v)
        worth = {}
        for item_id, vals in seen.items():
            w = current_worth(vals, v["fetched_at"])
            if w is not None:
                worth[item_id] = w["median"]
        days[present.local_date(v["fetched_at"])] = (v["fetched_at"], worth)
    return list(days.values())


# A front-edge spine is drawn this wide for its height: slim like a real case's, with
# room for its title across it. A spine photo is drawn at its own proportions.
EDGE_RATIO = 0.13
RATIO_BOUNDS = (0.04, 0.3)


def spine_face(it) -> tuple[str, float]:
    """('photo', width over height) when the shelf shows the case's spine photo, else
    ('edge', EDGE_RATIO) for the front cover's edge.

    Automatic (no `spine_choice`) shows the photo when the case was found in it and
    its print reads at shelf size; the owner's choice overrides that either way, though
    'photo' needs a crop to show.
    """
    cut = bool(it["spine_box"]) and bool(it["spine_ratio"])
    choice = it["spine_choice"]
    if cut and (choice == "photo" or (choice is None and it["spine_reads"])):
        lo, hi = RATIO_BOUNDS
        return "photo", round(min(hi, max(lo, it["spine_ratio"])), 4)
    return "edge", EDGE_RATIO


def spine_check(it) -> str | None:
    """Why an automatic spine photo needs a look, or None when it does not (or the
    owner has already chosen, or there is no spine photo)."""
    if it["spine_choice"] is not None or it["spine_box"] is None:
        return None
    if it["spine_box"] == "":
        return "No case found in the spine photo."
    if not it["spine_reads"]:
        return "Too faint to read at shelf size."
    if (it["spine_ratio"] or 0) > SLIP_RATIO:
        return "Wider than a case: shot in its slipcover."
    return None
