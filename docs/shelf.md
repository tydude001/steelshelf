# The shelf

How the library page is built: sorting and search, the film data behind the
genre and director groups, the tiles and chart above it, the spines on it,
`/stats`, and the shelf as a spreadsheet, out and in.

## Sorting and search

The shelf sorts by newest, title, value, label, format, region, genre or
director (`?sort=`, remembered in a cookie; `shelf.py`). The five grouping
sorts give each group its own run of shelf with a brass plaque naming it, its
count and its total. `?q=` searches title, edition, retailer, region, format,
notes, genre and director (every word, any field, ignoring case).

## Genre and director

Genre (the film's main one, so each case sits on one shelf) and director come
from TMDB by title (`film.py`) — the most popular result titled exactly as the
case, since TMDB ranks a new sequel first, else its top result; looked up in a
background thread after a save and at startup for any item not yet looked up.
Typing either field on the add or edit form sets the case by hand, and TMDB
never touches it again; clearing both hands it back. A retitle refetches what
the old title filled, and **Look up every film again** on `/stats` refetches
every case not set by hand. The item page links the TMDB match, so a wrong
film is one tap to see.

## Tiles and the value chart

Above the shelf, the value is the headline (`present.money`: a symbol for
USD, GBP and EUR, the code for any other), with the steelbook count, what it is
made of and the gain on what was paid under it; beside it, tiles for how many
prices are sales (desktop only), the largest label, and the items that need a
look, in amber. How the numbers are made folds away under them. Beside the
shelf from 960px
(under it on a phone): the five most valuable, and the shelf's value over
time — the total after each day with a fetch, walked from every valuation the
way the total is summed, so a fetch that found nothing drops its item there
too — with, dashed beside it, what was paid for the cases that total counts:
buying a case lifts both lines, a price that rose lifts only the value.

## Spines

Spines are the case's own spine photo where it reads (`spine.py`): at save,
and for older items at startup, a detector straightens the photo and finds the
case as the columns and rows that are smooth or unlike the carpet's colour,
then cuts it at its true width (12–24 px on the shelf, against 36 for the old
front-edge strips). A spine too faint to read at shelf size, or none found,
shows the front cover's edge instead, narrowed to match; `/review` lists those
(and slipcover spines) under **Spines to check**, and `/items/{id}/spine`
picks either by hand and redraws the crop.

Tuned on 72 real spine photos: the case is found in 71 (the miss was gold
print on a brown carpet); 13 are too faint and show the front edge; 3 are
flagged wider than a case, two slipcovers and one crop that spilled onto the
carpet.

## Stats

`/stats` (linked from the header) is the shelf in numbers: value by retailer
or label, how many prices are solid / fair / thin with the thin ones listed,
and counts by format, region and condition (`stats.py`).

## Export and import

**Export.** `GET /export.csv` (`csvio.py`, linked from `/stats`) downloads
the whole shelf, one row per case, oldest first, as
`steelshelf-<date>.csv`. The case's own columns come first under their
`items` field names — `id`, `title`, `year`, `format`, `edition`, `retailer`,
`region`, `upc`, `condition`, `notes`, `genre`, `director`,
`edition_keywords`, `search_query`, `paid_price`, `paid_on` — so the file
reads back into an import with no mapping. After them, what the case is
worth: `worth`, `worth_currency`, `worth_low`, `worth_high`, `worth_count`,
`worth_confidence` and `priced_on` from the valuation the shelf counts
(`current_worth`, so a fetch that found nothing exports no worth, as the
shelf shows none), with `worth_basis` `ask` or `sold` and the raw
`worth_source`: an ask is labelled an ask in the file as on the page. `added`
and `priced_on` are local dates, like the pages'. The price history stays
out: one row per case is what a spreadsheet holds, and the history lives in
`valuations`, which a database backup keeps ([deploy.md](deploy.md)).

The file opens with a UTF-8 byte-order mark, so Excel reads an accented title
right. A text cell opening with `=`, `+`, `-` or `@` gets a leading `'`,
which spreadsheets show as text instead of running it: a title or edition can
come from a web search. The UPC is text and keeps its leading zero in the
file, though a spreadsheet that reads it as a number drops it. Export is
behind the admin login although the pages are open: one file is the whole
record of what someone owns and paid.

**Import** reads a collector's existing spreadsheet into the shelf
(`csvio.read` and the import routes in `main.py`):

1. **Upload** (`POST /import`, admin, on `/stats` beside the export, and
   there on an empty shelf too): a CSV of up to 5,000 rows and 10 MB, its
   delimiter sniffed (comma, semicolon or tab), read as UTF-8 with or without
   the mark, else Windows-1252. The file is held under
   `PHOTO_DIR/_imports/<token>.csv` and pruned after a day, like a draft; no
   table.
2. **Map** columns to fields (`POST /import/<token>/map`), each guessed from
   its header (`csvio.ALIASES`, the header lower-cased with every run of
   punctuation or space read as `_`) — `Title`, `Movie` and `Film` to
   `title`, `Barcode`, `EAN` and `UPC/EAN` to `upc`, `Cost`, `Price paid`
   and `Purchase price` to `paid_price`, `Store` and `Exclusive` to
   `retailer`, and so on — and a steelshelf export maps itself by name. The
   worth columns and `id` map to nothing, and so does a bare `Price`, which
   may be paid, asked or hoped for. The first column to claim a field keeps
   it. The mapping is a form on the preview, folded away when a column was
   guessed as the title, open when none was: a file with no title column is
   held, not refused, and nothing imports until one is picked. A correction
   is held beside the file as `<token>.json`; two columns on one field, or a
   field that is not an import field, is refused.
3. **Preview** (`GET /import/<token>`) the first 50 rows to add and every
   row's problems, checked by the add form's own rules (`_item_values`,
   `_item_errors`) — no title, a UPC that is not digits, a paid price that is
   not a number, a year out of range — and the GS1 check digit identify holds
   a UPC to: a UPC of digits that fails it is dropped with a note, as
   identify drops one, and the row still imports. A UPC of 11 digits that
   passes once a zero is put back in front is a spreadsheet's doing and is
   repaired; a paid price with a comma and one or two digits after it
   (`12,50`, as a semicolon-separated European sheet writes it) is a decimal
   comma, since a thousands separator groups three; a leading `'` before
   `=`, `+`, `-` or `@` is the export's and comes off. Each repair is listed.
   A row whose UPC, or title, edition and retailer (ignoring case), matches a
   case on the shelf is marked a repeat and left out unless ticked, so
   importing an export onto the shelf it came from adds nothing. The columns
   that map to nothing — `id`, `added`, the worth columns — are named.
4. **Import** (`POST /import/<token>`) the good rows and the ticked repeats in
   one transaction, checked again from the held file rather than taken from
   the form, which is then deleted; the bad ones are listed, not saved. A
   typed genre or director marks the case set by hand, as on the add form.

Then the background work the app already does: TMDB fills genre, director
and year for every new case (`look_up_films`), and the import starts the
re-price by hand (**Run the rest now**), whose plan takes never-priced cases
first — so pricing a 300-case import is the existing job within the existing
quota, and whatever SerpApi's reserve leaves unreached carries to the monthly
run. There is no model call per row: identify reads photos, and an imported
case has none.

Two consequences. **An imported case has no photos**: the shelf draws it as
a plain spine with its title, and its item page, `/stats` and `/review`
render as usual (checked against a case saved with none), but nothing yet
adds photos to a saved case — that affordance, which would also run the spine
cut and **Identify again**, is the step after import. And **a price in the
spreadsheet is never imported as a valuation**: a number in someone's sheet
may be what they paid, an ask, a sale or a guess, and a valuation has to say
which; mapped to `paid_price` it is what was paid, and otherwise it stays in
the sheet.

**Built:** all four steps above, with the TMDB lookup after it and the
re-price left to **Run the rest now**, which the finished import points to.
Not built: adding photos to a saved case.
