# The shelf

How the library page is built: sorting and search, the film data behind the
genre and director groups, the tiles and chart above it, the spines on it, and
`/stats`.

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

Above the shelf, a row of tiles: the shelf's value, what was paid and the
gain, the largest label, and the items that need a look. Beside it from 960px
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
