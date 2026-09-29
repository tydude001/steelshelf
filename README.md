<h1><img alt="steelshelf" src="design/logo/steelshelf-banner.svg" width="340"></h1>

[![CI](https://github.com/tydude001/steelshelf/actions/workflows/ci.yml/badge.svg)](https://github.com/tydude001/steelshelf/actions/workflows/ci.yml)
[![Python 3.12](https://img.shields.io/badge/python-3.12-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)

Photograph a steelbook, log it in your library, and see what it is worth.
steelshelf is a self-hosted web app for steelbook collectors: point your phone
at a case, let Claude name the edition and the retailer exclusive, and watch
the shelf's value from eBay listings over time. FastAPI + Jinja + HTMX,
SQLite, one Docker container, no CDN.

<p>
  <img alt="The shelf on a desktop: twenty steelbook spines on a plank, value tiles above, the most valuable beside" src="docs/img/shelf.png" width="620">
  <img alt="The shelf on a phone" src="docs/img/phone.png" width="190">
</p>
<p>
  <img alt="An item page: the case, its photos, what it is worth against what was paid" src="docs/img/item.png" width="405">
  <img alt="The stats page: value by retailer, how sure the prices are, format, region and condition" src="docs/img/stats.png" width="405">
</p>

Every screenshot is the demo shelf (`scripts/demo.py`): invented titles and
drawn cases, since real steelbook art belongs to the studios and artists.

## Quick start

```bash
git clone https://github.com/tydude001/steelshelf.git && cd steelshelf
cp .env.example .env          # set ADMIN_PASSWORD, and the keys you want (§ Keys)
docker compose up -d --build  # http://localhost:8010
```

Then add your first case from the **+** button, on your phone for the camera.
No keys at all still gives you a shelf you fill in by hand; each key adds one
thing (§ Keys). [docs/deploy.md](docs/deploy.md) covers exposure, backups,
health checks and the optional Claude Code worker.

To look around first, build the demo shelf — twenty invented steelbooks with
drawn photos and a few months of made-up prices — and run the app on it:

```bash
python -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python scripts/demo.py
DATABASE_PATH=demo/steelshelf.db PHOTO_DIR=demo/photos REPRICE_ENABLED=false \
    .venv/bin/uvicorn app.main:app --port 8010
```

## What it does

1. **Capture.** From the phone (PWA, `<input capture>`), take front, spine and
   back photos of a steelbook, and optionally an **other** detail shot — a
   limited-edition number, the bottom of a box set, a sticker. A **box set or
   slip** tick tells the model the photos show outer packaging, which it cannot
   reliably see for itself; the saved edition then always names it.
2. **Identify.** Two signals, used together:
   - **Barcode first.** Decode the UPC on the back or on the retailer sticker
     locally (`barcode.py`, zxing-cpp; the GS1 check digit confirms it, and
     the decode is taken over the digits Claude reads, with a doubt noted when
     they differ). eBay's Browse API searches by UPC directly
     (`item_summary/search?gtin=`), which lands on the exact product. Most
     steelbook cases carry no barcode — it is on the slip or a sticker — so a
     miss is usual: on a real shelf of 72 cases the decoder read every code
     Claude had read and none Claude had missed.
   - **Vision second.** Send the photos to Claude for title, format,
     retailer exclusive (Best Buy / Zavvi / Walmart / …) or boutique label
     (Manta Lab / Filmarena / … — spine logo and release code, numbered
     run into `edition`), region and condition
     notes — the things a barcode misses, and the whole answer when there is
     no readable code. Claude may run up to three web searches (Anthropic's
     server-side tool, $10 per 1,000 plus the results as input tokens) to
     place a release code, exclusive or numbered run the photos leave open.
     eBay's Browse API image search is a further signal to try.
3. **Log.** One `items` row, a `photos` row per photo. Edit anything the model got
   wrong before saving: **Identify from photos** holds the photos as a draft
   (`PHOTO_DIR/_drafts/<token>/`, pruned after a day), re-renders `/add` filled
   in with the model's doubts listed, and **Save** writes the item from the
   draft. A UPC that fails its GS1 check digit is dropped, not saved. A saved
   item's fields stay editable (**Edit** at the top of its page); its photos do not.
   **Identify again from photos** on the edit page re-runs identification on the
   stored photos and fills the form for review, listing what changed; `/review`
   lists the items saved without a format or region, the ones worth a rerun (a
   missing UPC is not flagged: most cases carry none).
   **You paid** and **Bought on** are typed, never identified; the item page
   sets the paid price against the worth, and the shelf sums both.
4. **Value.** A background job asks a pricing source for comps and appends a
   `valuations` row — never overwrites — so the shelf's total is a line over
   time, not a snapshot. Every fetched row keeps the listings it was counted
   from (`listings` table), shown on the item page with a link each. **Not
   this one** sets a listing aside by its eBay item id: the price is
   re-counted from the rest as a new row, and that id stays out of every
   later fetch, stored flagged so the page still shows what was set aside.
   The item page leads with the price as **Worth about**, what was paid and
   the gain beside it, the low/high range, and a three-block confidence cue
   (`pricing.confidence`: thin at four listings or fewer, solid at twelve or
   more close together, fair otherwise or when the range is wider than three
   quarters of the median). Its history is a line once there are two prices.
   **Monthly re-price** (`reprice.py`): on the 25th at 03:00 local a background
   thread re-prices every item not priced since, oldest price first, through
   the source **Refresh price** uses; with SerpApi it reads the free Account
   API first and stops with 20 of the month's searches left for refreshing by
   hand. A run writes only prices it found (`valuations.via = 'monthly'`): an
   item with no listings keeps its last price, a failed request is tried once
   more then skipped, and one that stopped short resumes daily once searches
   allow. A price typed in by hand is never re-priced over. Each run and each
   item's outcome is logged (`reprice_runs`, `reprice_items`) for the status
   card on `/stats`, the item page's note, and a hollow dot on the shelf's
   value chart for a run that stopped short. `REPRICE_*` in `.env` tunes it.
5. **Browse.** The shelf sorts by newest, title, value, label, format,
   region, genre or director (`?sort=`, remembered in a cookie; `shelf.py`).
   The five grouping sorts give each group its own run of shelf with a brass
   plaque naming it, its count and its total. Genre (the film's main one, so
   each case sits on one shelf) and director come from TMDB by title
   (`film.py`) — the most popular result titled exactly as the case, since
   TMDB ranks a new sequel first, else its top result; looked up in a background thread after a save and at startup
   for any item not yet looked up. Typing either field on the add or edit
   form sets the case by hand, and TMDB never touches it again; clearing both
   hands it back. A retitle refetches what the old title filled, and **Look up
   every film again** on `/stats` refetches every case not set by hand. The
   item page links the TMDB match, so a wrong film is one tap to see. `?q=`
   searches title, edition, retailer, region, format, notes, genre and
   director (every word, any field, ignoring case). Above the shelf, a
   row of tiles: the shelf's value, what was paid and the gain, the largest
   label, and the items that need a look. Beside it from 960px (under it on a
   phone): the five most valuable, and the shelf's value over time — the total
   after each day with a fetch, walked from every valuation the way the total
   is summed, so a fetch that found nothing drops its item there too.
   **Spines** are the case's own spine photo where it reads (`spine.py`): at
   save, and for older items at startup, a detector straightens the photo and
   finds the case as the columns and rows that are smooth or unlike the
   carpet's colour, then cuts it at its true width (12–24 px on the shelf,
   against 36 for the old front-edge strips). A spine too faint to read at
   shelf size, or none found, shows the front cover's edge instead, narrowed
   to match; `/review` lists those (and slipcover spines) under **Spines to
   check**, and `/items/{id}/spine` picks either by hand and redraws the crop.
   Tuned on 72 real spine photos: the case is found in 71 (the miss was gold
   print on a brown carpet); 13 are too faint and show the front edge; 3 are
   flagged wider than a case, two slipcovers and one crop that spilled onto
   the carpet. `/stats`
   (linked from the header) is the shelf in numbers: value by retailer or
   label, how many prices are solid / fair / thin with the thin ones listed,
   and counts by format, region and condition (`stats.py`).

## Keys

`scripts/set-secrets.sh` prompts for each one (hidden input, Enter keeps the
current value), checks it with a real call, and writes `./.env`, mode 600;
`--remote user@host:/path/to/clone` writes that clone's `.env` over ssh
instead. Keep a copy of every secret in a password manager. Never in git.

- **eBay** — a Production keyset.
  1. Sign in at <https://developer.ebay.com> (a developer account, separate
     from the buying account; approval can take a business day).
  2. **Application Keysets** → create one named `steelshelf`, **Production**.
  3. A Production keyset stays disabled until the Marketplace Account
     Deletion requirement is met. steelshelf keeps no eBay user data, so
     take the exemption: **Alerts & Notifications** → "not persisting eBay
     data".
  4. Copy **App ID (Client ID)** and **Cert ID (Client Secret)**. The Dev ID
     is not used. The app mints its own application token.
- **Anthropic** — an API key for the vision step. Not the Max subscription:
  it doesn't cover API calls, and Anthropic reserves subscription sign-in for
  its own apps (<https://code.claude.com/docs/en/legal-and-compliance>).
  1. <https://console.anthropic.com> → **API Keys** → **Create Key**, named
     `steelshelf`. It is shown once — into your password manager before closing the dialog.
  2. The account needs credits (**Billing**). The script's check proves the
     key and the model, not the balance.
  3. `ANTHROPIC_MODEL` defaults to `claude-sonnet-5`; the script 404s on a model
     the key can't see.
- **Claude Code worker** (optional) — `WORKER_URL` and `WORKER_SECRET`, the
  bearer secret in the worker machine's `~/.config/steelshelf-worker.env`.
  Run on that machine, the script reads it from there.
- **SerpApi** (optional) — `SERPAPI_KEY`, and `SOLD_LOOKUP_ENABLED=true` to
  show the sold button and, with no eBay keyset, to fetch asks through it
  (§ Pricing source says why it is off by default). Free account at <https://serpapi.com>, key under
  **Api Key**. The script checks it against the Account API, which spends
  none of the month's 250 searches.
- **TMDB** (optional) — `TMDB_API_KEY`, the genre and director behind the
  "By genre" / "By director" sorts: a free key from
  <https://www.themoviedb.org/settings/api>. Either
  credential TMDB issues works — the v3 API key (sent as `api_key`; httpx's
  request log is held at WARNING, so it stays out of the logs) or the v4 read
  access token (a bearer). Unset, nothing is looked up and both are typed on
  the edit form. The script checks it against `/authentication`. With a key
  set, every page's footer carries TMDB's logo and "This product uses the
  TMDB API but is not endorsed or certified by TMDB.", as their terms require.
- **Admin** — `ADMIN_USER` / `ADMIN_PASSWORD`, HTTP Basic for every write and
  third-party call. The script generates the password if you press Enter, and
  the app logs a warning at startup while it is still `changeme`. **Reads are
  open**: every page and photo answers anyone who can reach the port, and
  Basic auth is plain text on plain HTTP. Run it on a LAN or a tailnet, not
  the open internet.

## Privacy

Your photos and your database stay on your machine. Photos leave it only when
you press **Identify**, to Anthropic's API or to your own Claude Code worker.
Titles go to TMDB for genre and director; titles and UPCs go to eBay (and
SerpApi, if you switch sold lookups on) for prices. Nothing is sent anywhere
else, and there is no telemetry.

## Vision model — the decision, 2026-09-24

**Claude does the vision.**

- **Vision.** Naming the retailer exclusive and the edition is recognition
  more than reading, and a 7B local VLM (Qwen2.5-VL, say) has little of that
  knowledge. Claude does it.
- **Which Claude, 2026-09-25.** Claude Code on the worker first, on your own
  Claude subscription: `tools/worker.py` runs it headless (`claude -p --restricted`,
  Read and WebSearch only) with the app's prompt and schema, in about 15–20 s.
  When the worker is off, logged out or over its limits, the app falls back to the
  API key — cents per item — and the form says which one answered. Running the
  `claude` CLI from a script stays inside Claude Code; the subscription's login
  is never lifted into the app itself, which the terms forbid (§ Keys). The
  worker is optional and runs *your own* `claude` login on *your own* machine;
  whether that use fits your plan's terms is between you and Anthropic.

`identify.py` puts the model behind an `Identifier` interface, like pricing,
so a local model or a barcode decoder can replace or join it without touching
the routes.

## Pricing source — the decision, 2026-09-24

eBay shut down the Finding API's completed-items call in February 2025. Sold
prices now live only in the Marketplace Insights API, a limited-release API
that needs eBay business approval and, per the developer forums, is not
granted to hobby developers. The Browse API returns active listings only.

**v1 uses active listings from the Browse API** (`source = 'ebay_active'`):
free and within eBay's terms. It reports the low / median / high of current
Buy It Now asks — a floor, not a sold price, and wrong on the high side for
hyped titles where asks run above what clears. The pricing layer is one
module behind an interface so a sold-data source can replace it later. The
alternatives considered, in the order they would be tried:

| Option | Why not (yet) |
|--------|---------------|
| Paid sold-comps API (several vendors resell scraped sold data) | Built 2026-09-25 as `serpapi_sold`, disabled by default — below |
| Apply for Marketplace Insights | Cheap to try; expect no answer |
| Scrape eBay's sold-and-completed search page | Against eBay's terms and brittle; if ever added it ships disabled |

**Sold prices can also be typed in by hand** (`source = 'manual_sold'`): look
the item up on eBay's site with the Sold Items filter and enter what it
cleared at on the item page. A person browsing is within eBay's terms, and
it is the one sold source with no third party in it. Added 2026-09-25 while
the developer account was rejected; it stays alongside the API rows, not
instead of them.

**Sold lookups through SerpApi** (`source = 'serpapi_sold'`), added the same
day. Every sold-comps vendor surveyed scrapes eBay's sold search — none
licenses it — so this is the scraper row above at one remove and **ships
disabled** (`SOLD_LOOKUP_ENABLED=false`). SerpApi was picked for the largest
free tier (250 searches/month, one per lookup) and a sold filter in its own
docs; SoldComps ($9/month, 100 free) and OpenWeb Ninja (pay-as-you-go) were
the runners-up. The search is keyword-only (title + "steelbook", + "4K"),
filtered by `keyword_match`, US dollars only. Retailer and region stay out
of the query, since many listings omit them and a narrower search risks zero
sales; instead `prefer_edition` keeps only the sales naming the item's retailer
and region, then retailer, then region, when at least three do, so one film's
other steelbooks don't set the median. US never narrows: on ebay.com it is
the default and goes unsaid. With no eBay keyset, the same switch also sends
"Fetch eBay asks" through SerpApi's current listings (`serpapi_active`, Buy It
Now and best-offer only; auctions dropped), filtered the same way. An opened
item is priced from used listings when three or more are in those results
(`serpapi_active_used`); otherwise the page says it is the price of sealed
copies. A used-only search is not run: for one popular title it found three.

## Layout

- `app/` — FastAPI app. `main.py` (routes, lifespan — the route table is its
  docstring), `auth.py` (HTTP Basic on every write and third-party call; fails
  closed with 503 until `ADMIN_USER` / `ADMIN_PASSWORD` are set), `photos.py`
  (uploads on disk as `PHOTO_DIR/<item id>/<kind>-<hex>.<ext>`, stored relative
  so host and container paths both resolve; derived images under
  `PHOTO_DIR/_derived/` — a 480px thumbnail per photo, which is what every
  page shows, a front-edge strip per item, and the spine cut from the spine
  photo — made at save, backfilled at startup off the request path, made on
  first request when missing), `spine.py` (finds the case in a spine photo,
  cuts it, and judges whether its print reads at shelf size),
  `reprice.py` (the monthly re-price: when it is due, the run, its log),
  `film.py` (TMDB by title: the main genre and the director),
  `barcode.py` (zxing-cpp decode of the UPC off the photos), `shelf.py` (the
  sort keys and grouped shelves, search, most valuable, value over time),
  `stats.py` (what `/stats` counts),
  `present.py` (dates as the local date only — SQLite stamps are UTC, and the
  container is UTC too unless `TZ` is set in `.env` — and
  the chart geometry the templates draw as inline SVG), `db.py` (schema:
  `items`, `photos`, `valuations`, `listings`, and the re-price log
  `reprice_runs` / `reprice_items`), `settings.py` (env via pydantic-settings),
  `pricing.py` (the `PricingSource` interface; `EbayActive`, by UPC;
  `EbayKeyword`, which the app uses — UPC when there is one, else a title
  search filtered to steelbooks of that title and format, saved as
  `source = 'ebay_keyword'`; `value_item`, which appends a `valuations`
  row; `SerpApiSold` / `SerpApiActive`, sold listings and asks by title, off
  unless enabled; and
  `parse_prices` / `record_sold`, for sold prices typed in), `identify.py` (the `Identifier`
  interface; `ClaudeVision`: photos downscaled to 2576px, one Messages API
  call with a JSON-schema output, web search capped at 3, a paused search
  turn resumed, server-side refusal fallbacks on; `ClaudeCodeWorker`, the
  worker's client; `Fallback`, the worker then the API),
  `templates/` (the library is a CSS bookcase: one wrapping row of
  same-height steelbook spines per shelf run, each as wide as its `--r`, a
  repeating background painting a plank under each wrapped line; a spine is
  its spine photo, or else a strip cut from the left edge of its front photo
  — `photos.spine_strip`, the way a real case's art wraps round — over the
  case colour from `photos.spine_color`; that strip assumes the case fills
  the middle of the frame, `CASE_BOX`), `static/` (`steelshelf.css`; the icon — three
  steelbook spines and a disc on a shelf — as `mark.svg` for the header,
  manifest and PWA PNGs, and `icon.svg`, a simpler two-spine cut that still
  reads at 16 px, for the browser tab and `favicon.ico`; the touch and
  maskable masters live in `design/icon/`, and `scripts/render-icons.py`
  renders every PNG and the ICO from the SVGs (`scripts/render-lockup.py`
  builds the lockups in `design/logo/`, and this README's header, a pale
  banner that reads on light and dark themes alike); Fraunces and Figtree vendored under
  `fonts/` with their OFL licences, so page loads stay CDN-free). Every
  colour in `steelshelf.css` is a `:root` token, redefined under
  `prefers-color-scheme: dark`; `tests/test_theme.py` holds the sheet to it.
- `data/` — SQLite (`steelshelf.db`) + `photos/`. **Git-ignored** but for an
  empty `.gitkeep`, so compose has a mount source.
- `tools/` — run on the machine logged in to Claude Code, not in the image. `worker.py` is the identify
  worker (user unit `steelshelf-worker`, `:8012`, bearer secret); its docstring
  has the install.
- `scripts/` — `demo.py` (the demo shelf), `set-secrets.sh` (§ Keys), and
  the icon and lockup renderers.
- `tests/` — pytest over a throwaway SQLite DB per test (`conftest.py`); no
  network, no real `data/`.
- `Dockerfile`, `docker-compose.yml`, `requirements*.txt`, `.env.example`.

## Development

App dependencies live in the Docker image; for tests and a dev server, a
local `.venv`:

```bash
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m ruff check . && .venv/bin/python -m pytest -q
cp .env.example .env
.venv/bin/uvicorn app.main:app --reload --port 8010
```

The suite needs no network and no keys. [CONTRIBUTING.md](CONTRIBUTING.md)
has the rules a pull request is checked against.

## Licence and credits

[MIT](LICENSE). Security reports: [SECURITY.md](SECURITY.md).

Fraunces and Figtree are vendored under the SIL Open Font License
(`app/static/fonts/`). Film data comes from TMDB: this product uses the TMDB
API but is not endorsed or certified by TMDB. steelshelf is not affiliated
with eBay, TMDB, SerpApi or Anthropic. SteelBook® is a trademark of Scanavo,
used here only to describe the cases the app catalogues.
