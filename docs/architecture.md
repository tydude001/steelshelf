# Architecture

Where things live. The route table is `app/main.py`'s docstring; the design
decisions are in [identification.md](identification.md) and
[pricing.md](pricing.md); the rules a change is held to are in
[CONTRIBUTING.md](../CONTRIBUTING.md).

## Layout

- `app/` — FastAPI app.
  - `main.py` — routes, lifespan; the route table is its docstring.
  - `auth.py` — HTTP Basic on every write and third-party call; fails closed
    with 503 until `ADMIN_USER` / `ADMIN_PASSWORD` are set.
  - `photos.py` — uploads on disk as `PHOTO_DIR/<item id>/<kind>-<hex>.<ext>`,
    stored relative so host and container paths both resolve; derived images
    under `PHOTO_DIR/_derived/` — a 480px thumbnail per photo, which is what
    every page shows, a front-edge strip per item, and the spine cut from the
    spine photo — made at save, backfilled at startup off the request path,
    made on first request when missing.
  - `spine.py` — finds the case in a spine photo, cuts it, and judges whether
    its print reads at shelf size.
  - `reprice.py` — the monthly re-price: when it is due, the run, its log.
  - `film.py` — TMDB by title: the main genre and the director.
  - `barcode.py` — zxing-cpp decode of the UPC off the photos.
  - `shelf.py` — the sort keys and grouped shelves, search, most valuable,
    value over time.
  - `stats.py` — what `/stats` counts.
  - `present.py` — dates as the local date only (SQLite stamps are UTC, and
    the container is UTC too unless `TZ` is set in `.env`), and the chart
    geometry the templates draw as inline SVG.
  - `db.py` — schema: `items`, `photos`, `valuations`, `listings`, and the
    re-price log `reprice_runs` / `reprice_items`.
  - `settings.py` — env via pydantic-settings.
  - `pricing.py` — the `PricingSource` interface; `current_worth`, which
    valuation an item is worth; `EbayActive`, by UPC; `EbayKeyword`, which the
    app uses — UPC when there is one, else a title search filtered to
    steelbooks of that title and format, saved as `source = 'ebay_keyword'`;
    `value_item`, which appends a `valuations` row; `TitleSearched`, the UPC →
    own search → title pipeline behind `SerpApiSold` / `SerpApiActive` and
    `SoldComps`, all off unless enabled; `narrow_edition`, the judge then the
    word tiers; and `parse_prices` / `record_sold`, for sold prices typed in.
  - `judge.py` — which listings are the item's edition: `ClaudeJudge`,
    `OpenAIJudge`, `WorkerJudge`, `FallbackJudge`, and `ListingJudge`, which
    the sources hold.
  - `identify.py` — the `Identifier` interface; `ClaudeVision`: photos
    downscaled to 2576px, one Messages API call with a JSON-schema output, web
    search capped at 3, a paused search turn resumed, server-side refusal
    fallbacks on; `OpenAIVision`, the same through `chat.py` when
    `AI_PROVIDER=openai`, with the search rules cut from the prompt unless
    OpenRouter's search is on; `ClaudeCodeWorker`, the worker's client;
    `Fallback`, the worker then the API.
  - `chat.py` — `ChatClient`, one POST to an OpenAI-compatible
    `/chat/completions` (OpenAI, OpenRouter, Ollama…) with a JSON schema,
    the answer read leniently; the other route beside the Anthropic SDK.
  - `templates/` — the library is a CSS bookcase: one wrapping row of
    same-height steelbook spines per shelf run, each as wide as its `--r`, a
    repeating background painting a plank under each wrapped line. A spine is
    its spine photo, or else a strip cut from the left edge of its front photo
    — `photos.spine_strip`, the way a real case's art wraps round — over the
    case colour from `photos.spine_color`; that strip assumes the case fills
    the middle of the frame, `CASE_BOX`.
  - `static/` — `steelshelf.css`; the icon — three steelbook spines and a disc
    on a shelf — as `mark.svg` for the header, manifest and PWA PNGs, and
    `icon.svg`, a simpler two-spine cut that still reads at 16 px, for the
    browser tab and `favicon.ico`; Fraunces and Figtree vendored under
    `fonts/` with their OFL licences, so page loads stay CDN-free. Every
    colour in `steelshelf.css` is a `:root` token, redefined under
    `prefers-color-scheme: dark`; `tests/test_theme.py` holds the sheet to it.
- `design/` — the touch and maskable icon masters (`design/icon/`) and the
  lockups (`design/logo/`), including the README's header, a pale banner that
  reads on light and dark themes alike. `scripts/render-icons.py` renders
  every PNG and the ICO from the SVGs; `scripts/render-lockup.py` builds the
  lockups.
- `data/` — SQLite (`steelshelf.db`) + `photos/`. **Git-ignored** but for an
  empty `.gitkeep`, so compose has a mount source.
- `tools/` — run on the machine logged in to Claude Code, not in the image.
  `worker.py` is the identify and listing-judge worker (user unit
  `steelshelf-worker`, `:8012`, bearer secret); its docstring has the install.
- `scripts/` — `demo.py` (the demo shelf), `set-secrets.sh` and `set-key.sh`
  (README § Keys; their live checks in `checks.sh`), and the icon and lockup
  renderers.
- `tests/` — pytest over a throwaway SQLite DB per test (`conftest.py`); no
  network, no real `data/`.
- `docs/` — these pages, [deploy.md](deploy.md), and the README's
  screenshots under `img/`, all taken from the demo shelf.
- `Dockerfile`, `docker-compose.yml`, `docker-compose.demo.yml`,
  `requirements*.txt`, `.env.example`.
