"""The shelf as a spreadsheet: one CSV row per case (`GET /export.csv`).

Pure functions over `db.list_items` rows; nothing here touches the database or
the network. Import, the other half, is designed in docs/shelf.md § Export and import.

The item's own columns carry the `items` field names, so a later import reads
this file back without a mapping step. After them, what the case is worth — the
valuation `pricing.current_worth` picks, the one the shelf counts — with
`worth_basis` saying whether that is an ask or a sold price: an ask is never
passed off as a sale, in a file any more than on a page.
"""

import csv
import io

from app import present
from app.pricing import SOLD_SOURCES, confidence

# The case as the owner and the identify step left it, in `items` field names.
ITEM_COLUMNS = ("id", "title", "year", "format", "edition", "retailer", "region", "upc",
                "condition", "notes", "genre", "director", "edition_keywords", "search_query",
                "paid_price", "paid_on")
# What it is worth: derived, never read back by an import.
WORTH_COLUMNS = ("added", "worth", "worth_currency", "worth_basis", "worth_low", "worth_high",
                 "worth_count", "worth_confidence", "worth_source", "worth_matched",
                 "priced_on")
COLUMNS = ITEM_COLUMNS + WORTH_COLUMNS

# A spreadsheet runs a cell that opens with one of these as a formula. A title or
# edition can come from a web search, so text that does gets a leading apostrophe,
# which spreadsheets show as plain text; an import strips it again.
FORMULA_LEAD = ("=", "+", "-", "@", "\t", "\r")


def _text(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (int, float)):
        return format(value, "g") if isinstance(value, float) else str(value)
    text = str(value)
    return "'" + text if text.startswith(FORMULA_LEAD) else text


def _money(value: float | None) -> str:
    return "" if value is None else f"{value:.2f}"


def row(item) -> dict[str, str]:
    """One case as {column: cell}, every column present, blank where it has nothing."""
    out = {c: _text(item[c]) for c in ITEM_COLUMNS}
    out["paid_price"] = _money(item["paid_price"])
    out["added"] = present.local_date(item["created_at"]) if item["created_at"] else ""
    median = item["latest_median"]
    if median is None:
        return {**out, **{c: "" for c in WORTH_COLUMNS if c != "added"}}
    source = item["latest_source"]
    cue = confidence(item["latest_n"], item["latest_low"], item["latest_high"], median,
                     "sale" if source in SOLD_SOURCES else "listing", item["latest_matched"])
    out.update(
        worth=_money(median), worth_currency=item["latest_currency"] or "",
        worth_basis="sold" if source in SOLD_SOURCES else "ask",
        worth_low=_money(item["latest_low"]), worth_high=_money(item["latest_high"]),
        worth_count=_text(item["latest_n"]), worth_confidence=cue.word if cue else "",
        worth_source=source or "", worth_matched=item["latest_matched"] or "",
        priced_on=(present.local_date(item["latest_fetched_at"])
                   if item["latest_fetched_at"] else ""),
    )
    return out


def export(items) -> str:
    """The whole shelf as CSV text, oldest case first, with a byte-order mark so
    Excel reads it as UTF-8 (a title with an accent survives the round trip)."""
    buf = io.StringIO()
    buf.write("﻿")
    writer = csv.DictWriter(buf, fieldnames=COLUMNS)
    writer.writeheader()
    for item in sorted(items, key=lambda it: it["id"]):
        writer.writerow(row(item))
    return buf.getvalue()
