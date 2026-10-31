"""The shelf as a spreadsheet, out and in: one CSV row per case.

Export (`GET /export.csv`) and import (`POST /import`, docs/shelf.md § Export and
import). Pure functions; nothing here touches the database or the network — the
routes in `main.py` check each imported row with the add form's own rules and
save it.

The item's own columns carry the `items` field names, so a later import reads
this file back without a mapping step. After them, what the case is worth — the
valuation `pricing.current_worth` picks, the one the shelf counts — with
`worth_basis` saying whether that is an ask or a sold price: an ask is never
passed off as a sale, in a file any more than on a page.
"""

import csv
import io
import re
from dataclasses import dataclass, field

from app import present
from app.identify import upc_valid
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


# --- import -----------------------------------------------------------------------
# This slice reads a file laid out like the export: its columns map to fields by
# name. `id` and the worth columns are read by nothing — a spreadsheet's price may be
# what was paid, an ask, a sale or a guess, and a valuation has to say which, so a
# price in the file is never saved as one.

IMPORT_FIELDS = tuple(c for c in ITEM_COLUMNS if c != "id")
MAX_ROWS = 5000
MAX_BYTES = 10 * 1024 * 1024
DELIMITERS = ",;\t"


class SheetError(ValueError):
    """A file that cannot be imported at all (not a row's problem)."""


@dataclass
class Sheet:
    fields: list[str]  # the IMPORT_FIELDS the header names, in file order
    ignored: list[str]  # header cells that map to nothing
    rows: list[dict[str, str]] = field(default_factory=list)  # {field: cell}, raw


def decode(data: bytes) -> str:
    """UTF-8 with or without the byte-order mark, else Windows-1252."""
    if data.startswith(b"\xef\xbb\xbf"):
        return data[3:].decode("utf-8", errors="replace")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        return data.decode("cp1252", errors="replace")


def _header_name(cell: str) -> str:
    return cell.strip().lower().replace(" ", "_")


def read(data: bytes) -> Sheet:
    """The file's rows keyed by field name; SheetError when it cannot be read as a
    steelshelf-layout CSV (empty, too big, no title column, too many rows)."""
    if len(data) > MAX_BYTES:
        raise SheetError(f"the file is over {MAX_BYTES // (1024 * 1024)} MB")
    text = decode(data)
    if not text.strip():
        raise SheetError("the file is empty")
    try:
        dialect = csv.Sniffer().sniff(text[:8192], delimiters=DELIMITERS)
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = ","
    reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter)
    header = next(reader, [])
    names = [_header_name(c) for c in header]
    if "title" not in names:
        raise SheetError("no title column: this import reads a steelshelf export, or a "
                           "sheet whose columns are named the same way")
    keep = {i: n for i, n in enumerate(names) if n in IMPORT_FIELDS}
    sheet = Sheet(fields=list(dict.fromkeys(keep.values())),
                  ignored=[c.strip() for i, c in enumerate(header)
                           if i not in keep and c.strip()])
    for cells in reader:
        if not any(c.strip() for c in cells):
            continue  # a blank line, as spreadsheets leave at the end
        if len(sheet.rows) == MAX_ROWS:
            raise SheetError(f"more than {MAX_ROWS:,} rows; split the file")
        row: dict[str, str] = {}
        for i, name in keep.items():
            if i < len(cells) and name not in row:
                row[name] = cells[i]
        sheet.rows.append(row)
    return sheet


def unformula(text: str) -> str:
    """Undo the export's defusing: a leading apostrophe before a formula character."""
    if len(text) > 1 and text[0] == "'" and text[1:].startswith(FORMULA_LEAD):
        return text[1:]
    return text


def repair_upc(upc: str | None) -> tuple[str | None, str | None]:
    """(the UPC to save, a note on what was done to it, if anything).

    A spreadsheet that reads a UPC as a number drops its leading zero: 11 digits that
    pass the GS1 check once a zero is put back are repaired. A UPC of digits that
    fails its check digit is dropped with a note, as identify drops one; anything
    not digits is left for the add form's own check to refuse."""
    if not upc or not upc.isdigit():
        return upc, None
    if len(upc) == 11 and upc_valid("0" + upc):
        return "0" + upc, f"upc: {upc} had lost its leading zero — repaired"
    if not upc_valid(upc):
        return None, f"upc: {upc} fails its check digit — left blank"
    return upc, None


def repeat_key(title, edition, retailer) -> tuple[str, str, str]:
    """What makes two cases the same release, with no UPC to go by."""
    return tuple((v or "").strip().casefold() for v in (title, edition, retailer))


# A comma followed by one or two digits cannot be a thousands separator, which groups
# three: it is a decimal comma, as a European spreadsheet writes 12,50.
DECIMAL_COMMA = re.compile(r"([$€£]?\s*\d+),(\d{1,2})")


def decimal_comma(price: str | None) -> tuple[str | None, str | None]:
    """(the paid price with a decimal comma read as a point, a note when it was)."""
    m = DECIMAL_COMMA.fullmatch((price or "").strip())
    if not m:
        return price, None
    fixed = f"{m[1]}.{m[2]}"
    return fixed, f"paid price: {price} read as {fixed}, a decimal comma"
