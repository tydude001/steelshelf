"""steelshelf — FastAPI entry point.

  GET  /                   library; ?sort= newest|title|value|label|
                           format|region|genre|director, remembered
                           in a cookie; ?q= searches title, edition,
                           retailer, region, format, notes, genre and
                           director                                   (open)
  GET  /stats              the shelf in numbers: value by label, how
                           sure the prices are, format/region/condition (open)
  GET  /add                phone photo form: front / spine / back     (HTTP Basic)
  POST /add/identify       hold the photos as a draft, decode the barcode
                           locally, ask Claude what they show, re-render
                           /add filled in                              (HTTP Basic)
  POST /items              save an item and its photos (uploaded, or
                           held by a draft), 303 to it                (HTTP Basic)
  GET  /items/{id}         one item: photos, fields, valuation history (open)
  GET  /items/{id}/edit    the item's fields, to correct              (HTTP Basic)
  POST /items/{id}         save the edited fields, 303 to the item    (HTTP Basic)
  POST /items/{id}/identify ask Claude again from the stored photos,
                           re-render the edit form filled in          (HTTP Basic)
  GET  /review             items missing a format or region, and spine
                           photos the shelf could not use             (open)
  POST /items/{id}/value   fetch an eBay quote, append a valuation    (HTTP Basic)
  POST /items/{id}/sold    append sold prices typed in by hand         (HTTP Basic)
  POST /items/{id}/listings/{lid}/toggle  set one listing aside as not
                           this edition (or count it again); appends the
                           price re-counted without it                (HTTP Basic)
  POST /reprice            re-price what this month's run has not
                           reached, now, in the background            (HTTP Basic)
  POST /films/refetch      look every item up on TMDB afresh, in the
                           background; typed genres go too            (HTTP Basic)
  POST /items/{id}/sold/lookup  fetch sold eBay prices via SerpApi, append
                           a valuation; 404 unless SOLD_LOOKUP_ENABLED (HTTP Basic)
  GET  /photos/{id}        a stored photo, looked up by row id        (open)
  GET  /photos/{id}/thumb  its thumbnail, made on first request        (open)
  GET  /items/{id}/spine.jpg the front cover's edge, painted on the
                           shelf spine; made on first request          (open)
  GET  /items/{id}/spine-photo.jpg the case's spine cut from its spine
                           photo, painted on the shelf instead when it
                           reads (or you say so)                       (open)
  GET  /items/{id}/spine-frame.jpg the spine photo turned and
                           straightened, for the crop editor           (open)
  GET  /items/{id}/spine   which spine the shelf shows, and the crop  (HTTP Basic)
  POST /items/{id}/spine   choose it, save a crop, rotate, or look for
                           the case again                             (HTTP Basic)
  GET  /healthz            counts items; 503 if the DB is unreachable (Docker probe)
  GET  /favicon.ico        the icon, for browsers that ask the root   (open)
  GET  /static/...         icons, manifest, vendored fonts            (open)

Pages render from the database only. The third-party calls (identification,
pricing, TMDB) run from a POST or a background job (the monthly re-price,
`app/reprice.py`; the film lookup after a save, `app/film.py`), per the repo rule.
"""

import logging
import sqlite3
import threading
import zlib
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from urllib.parse import urlencode

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.concurrency import run_in_threadpool

from app import barcode, db, film, photos, present, reprice, shelf, spine, stats
from app.auth import require_admin
from app.identify import (
    BOXED_WORDS,
    FIELDS,
    ClaudeCodeWorker,
    ClaudeVision,
    Fallback,
    Identifier,
    IdentifyError,
)
from app.pricing import (
    MANUAL_SOLD,
    QUARTILES_FROM,
    SOLD_FRESH,
    SOLD_SOURCES,
    EbayKeyword,
    PricingError,
    PricingSource,
    SerpApiActive,
    SerpApiSold,
    SoldComps,
    confidence,
    current_worth,
    fits_condition,
    is_opened,
    parse_prices,
    record_sold,
    toggle_listing,
    value_item,
)
from app.settings import settings

log = logging.getLogger("steelshelf")
TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
TEMPLATES.env.filters["spine_ink"] = photos.spine_ink
TEMPLATES.env.filters["day"] = present.day
TEMPLATES.env.filters["short_day"] = present.short_day
TEMPLATES.env.filters["local_date"] = present.local_date
TEMPLATES.env.filters["spine_face"] = shelf.spine_face
# The genre and director fields say TMDB fills them only when it will.
TEMPLATES.env.globals["tmdb_on"] = lambda: bool(settings.tmdb_api_key)
TEMPLATES.env.globals["sold_sources"] = SOLD_SOURCES
TEMPLATES.env.globals["quartiles_from"] = QUARTILES_FROM
# A short tag for an image URL that changes when the image does (a new crop).
TEMPLATES.env.filters["ver"] = lambda text: format(zlib.crc32((text or "").encode()), "x")
STATIC = Path(__file__).parent / "static"


@asynccontextmanager
async def lifespan(app: FastAPI):
    logging.basicConfig(level=settings.log_level)
    # httpx logs every request URL at INFO, and SerpApi's key rides in the query string.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    db.init_db(settings.database_path)
    Path(settings.photo_dir).mkdir(parents=True, exist_ok=True)
    log.info("database ready at %s", settings.database_path)
    if settings.admin_password == "changeme":
        log.warning("ADMIN_PASSWORD is still .env.example's 'changeme': anyone who reads "
                    "the example can add, edit and delete. Set your own.")
    backfill_spine_colors()
    # Thumbnails and spine strips for items saved before they existed: a decode per
    # photo, so off the startup path; a page that asks first makes its own.
    threading.Thread(target=backfill_derived, name="derived", daemon=True).start()
    look_up_films()  # genre and director for items saved before they existed
    app.state.scheduler = reprice.Scheduler(
        settings.database_path, lambda: reprice_job(), settings.reprice_day,
        settings.reprice_hour,
        settings.reprice_reserve)
    app.state.scheduler.start()
    yield
    app.state.scheduler.stop.set()


def backfill_spine_colors() -> None:
    """Give every item saved before spine colours existed one from its front photo."""
    with db.connect(settings.database_path) as conn:
        todo = db.items_missing_spine_color(conn)
        for row in todo:
            path = photos.resolve(settings.photo_dir, row["path"])
            color = photos.spine_color(path.read_bytes()) if path else None
            db.set_spine_color(conn, row["item_id"], color)
        conn.commit()
    if todo:
        log.info("spine colours backfilled for %d items", len(todo))


def backfill_derived() -> None:
    made = 0
    with db.connect(settings.database_path) as conn:
        for row in db.all_photos(conn):
            path = photos.resolve(settings.photo_dir, row["path"])
            if path and not photos.thumb_path(settings.photo_dir, row["id"]).is_file():
                made += photos.ensure_thumb(settings.photo_dir, row["id"], path) is not None
        for row in db.front_photos(conn):
            path = photos.resolve(settings.photo_dir, row["path"])
            if path and not photos.spine_path(settings.photo_dir, row["item_id"]).is_file():
                made += photos.ensure_spine(settings.photo_dir, row["item_id"], path) is not None
        todo = db.items_missing_spine_box(conn)
    for row in todo:
        path = photos.resolve(settings.photo_dir, row["path"])
        if path:
            data = path.read_bytes()
            measure_spine(row["item_id"], data, spine.detect(data))
            made += 1
    if made:
        log.info("derived images backfilled: %d made", made)


_films: film.FilmSource | None = None


def get_films() -> film.FilmSource | None:
    """The TMDB client, or None with no TMDB_API_KEY set."""
    global _films
    if _films is None and settings.tmdb_api_key:
        _films = film.Tmdb(settings.tmdb_api_key)
    return _films


def fill_films() -> None:
    source = get_films()
    if source is None:
        return
    with db.connect(settings.database_path) as conn:
        matched = film.fill_items(conn, source)
        years = film.fill_years(conn, source) if hasattr(source, "year") else 0
    if matched:
        log.info("film lookup: %d matched on TMDB", matched)
    if years:
        log.info("film lookup: %d release years filled", years)


# One lookup at a time: a save during the startup backfill should not ask twice.
_film_lock = threading.Lock()


def look_up_films() -> None:
    """Fill genre and director from TMDB off the request, for every item not yet
    looked up; nothing with no token."""
    if get_films() is None:
        return

    def run():
        with _film_lock:
            fill_films()

    threading.Thread(target=run, name="films", daemon=True).start()


def measure_spine(item_id: int, data: bytes, box: spine.Box | None) -> None:
    """Cut the spine photo at `box` for the shelf, and record the box, its proportions
    and whether its print reads; no box (or one that will not cut) records none found."""
    strip = spine.cut(data, box) if box else None
    dest = photos.spine_photo_path(settings.photo_dir, item_id)
    with db.connect(settings.database_path) as conn:
        if strip is None:
            dest.unlink(missing_ok=True)
            db.set_spine_box(conn, item_id, "", None, None)
        else:
            photos.write_derived(dest, spine.strip_jpeg(strip))
            db.set_spine_box(conn, item_id, box.dumps(), round(strip.width / strip.height, 4),
                             spine.reads(strip))
        conn.commit()


def write_derived_for(item_id: int, saved: list[tuple[int, str, bytes]]) -> None:
    """Thumbnail each photo just saved, and the spine strip from the front."""
    for photo_id, kind, data in saved:
        photos.write_derived(photos.thumb_path(settings.photo_dir, photo_id),
                             photos.thumbnail(data))
        if kind == "front":
            photos.write_derived(photos.spine_path(settings.photo_dir, item_id),
                                 photos.spine_strip(data))
        elif kind == "spine":
            measure_spine(item_id, data, spine.detect(data))


app = FastAPI(title="steelshelf", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=STATIC), name="static")

_pricing: PricingSource | None = None


def asks_via_serpapi() -> bool:
    """Asks come from SerpApi only with no eBay keyset, and only while SerpApi is enabled."""
    has_keyset = settings.ebay_client_id and settings.ebay_client_secret
    return not has_keyset and settings.sold_lookup_enabled and bool(settings.serpapi_key)


def get_pricing() -> PricingSource:
    """One ask source per process; EbayKeyword so its application token is reused."""
    global _pricing
    if _pricing is None:
        if asks_via_serpapi():
            _pricing = SerpApiActive(settings.serpapi_key)
        else:
            _pricing = EbayKeyword(
                settings.ebay_client_id, settings.ebay_client_secret,
                settings.ebay_marketplace_id,
            )
    return _pricing


def reprice_job():
    """The monthly re-price's (source, SerpApi count reader): the source "Refresh
    price" uses, and a quota to read only when that is SerpApi. None when switched
    off or no source is set up (no eBay keyset, and SerpApi not enabled)."""
    if not settings.reprice_enabled:
        return None
    if asks_via_serpapi():
        return get_pricing(), lambda: reprice.searches_left(settings.serpapi_key)
    if settings.ebay_client_id and settings.ebay_client_secret:
        return get_pricing(), None
    return None


def reprice_status(conn, scheduler) -> dict:
    """What /stats and the item page say about the monthly re-price; the database
    and the clock only."""
    now = datetime.now().astimezone()
    last = reprice.last_run(conn)
    return {
        "on": reprice_job() is not None,
        "enabled": settings.reprice_enabled,
        "day": settings.reprice_day, "hour": settings.reprice_hour,
        "reserve": settings.reprice_reserve,
        "last": last,
        "missed": reprice.missed(conn, last),
        "due": reprice.due(conn, now, settings.reprice_day, settings.reprice_hour),
        "next": reprice.next_run(now, settings.reprice_day, settings.reprice_hour),
        "running": scheduler.running,
    }


_sold_pricing: PricingSource | None = None


def get_sold_pricing() -> PricingSource:
    """SoldComps when it has a key, SerpApi's sold search otherwise."""
    global _sold_pricing
    if _sold_pricing is None:
        if settings.soldcomps_key:
            _sold_pricing = SoldComps(settings.soldcomps_key)
        else:
            _sold_pricing = SerpApiSold(settings.serpapi_key)
    return _sold_pricing


_identifier: Identifier | None = None


def get_identifier() -> Identifier:
    """Claude Code on the worker when WORKER_URL is set, the API when it fails or is unset."""
    global _identifier
    if _identifier is None:
        api = ClaudeVision(settings.anthropic_api_key, settings.anthropic_model)
        _identifier = api
        if settings.worker_url:
            worker = ClaudeCodeWorker(settings.worker_url, settings.worker_secret)
            _identifier = Fallback(worker, api)
    return _identifier


def _clean(value: str | None) -> str | None:
    value = (value or "").strip()
    return value or None


@app.get("/", response_class=HTMLResponse)
def index(request: Request, sort: str | None = None, q: str = ""):
    chosen = sort if sort in shelf.SORTS else None
    remembered = request.cookies.get("sort")
    order = chosen or (remembered if remembered in shelf.SORTS else shelf.DEFAULT_SORT)
    with db.connect(settings.database_path) as conn:
        items = db.list_items(conn)
        n_review = len(db.items_to_review(conn))
        history = db.valuation_history(conn)
        status = reprice_status(conn, request.app.state.scheduler)
        marks = {present.local_date(stamp): kind
                 for stamp, kind in reprice.run_days(conn).items()}
    floor = _shelf_floor(items)
    q = q.strip()
    matched = shelf.search(items, q)
    # No match still shows the bookcase: one empty run of shelf, not a blank page.
    shelves = shelf.shelve(matched, order) if matched else [shelf.Shelf(None)]
    response = TEMPLATES.TemplateResponse(
        request, "index.html",
        {"items": items, "shelves": shelves, "sort": order, "q": q, "n_matched": len(matched),
         "sorts": shelf.SORTS, "grouped": shelf.SORTS[order][1],
         "floor": floor, "kinds": _price_kinds(items), "n_review": n_review,
         "basis": _basis(items),
         "biggest": shelf.shelve(items, "label")[0] if items else None,
         "top": shelf.most_valuable(items),
         "over_time": present.line(
             shelf.value_over_time(history, floor[0]["currency"]) if floor else [],
             width=320, height=100),
         "marks": marks, "reprice": status},
    )
    if chosen and chosen != remembered:
        response.set_cookie("sort", chosen, max_age=365 * 24 * 3600, samesite="lax")
    return response


@app.get("/stats", response_class=HTMLResponse)
def stats_page(request: Request):
    with db.connect(settings.database_path) as conn:
        items = db.list_items(conn)
        status = reprice_status(conn, request.app.state.scheduler)
    floor = _shelf_floor(items)
    ctx = {"items": items, "main": floor[0] if floor else None,
           "by_label": stats.value_by_label(items) if floor else [],
           "certainty": stats.certainty(items), "breakdowns": stats.breakdowns(items),
           "as_of": stats.as_of(items), "basis": _basis(items),
           "no_label": sum(1 for it in items if not (it["retailer"] or "").strip()),
           "reprice": status, "films": _film_counts(items)}
    return TEMPLATES.TemplateResponse(request, "stats.html", ctx)


def _film_counts(items) -> dict:
    """What /stats says about the TMDB lookups: on at all, matched, no match, set by
    hand, waiting."""
    ids = [it["tmdb_id"] for it in items]
    return {"on": get_films() is not None,
            "matched": sum(1 for i in ids if i is not None and i > 0),
            "none": sum(1 for i in ids if i == 0),
            "by_hand": sum(1 for i in ids if i == db.HAND_SET),
            "waiting": sum(1 for i in ids if i is None)}


@app.post("/films/refetch")
def films_refetch(_: str = Depends(require_admin)):
    """Look every item up on TMDB again, replacing what was filled; 404 with no key."""
    if get_films() is None:
        raise HTTPException(404, "TMDB lookups are off: set TMDB_API_KEY")
    with db.connect(settings.database_path) as conn:
        n = db.reset_all_films(conn)
    log.info("film lookup: all %d items to look up again", n)
    look_up_films()
    return RedirectResponse("/stats#films", status_code=303)


@app.post("/reprice")
def reprice_now(request: Request, _: str = Depends(require_admin)):
    """Re-price whatever this cycle has not reached, now, in the background."""
    started = request.app.state.scheduler.start_by_hand()
    log.info("re-price by hand: %s", "started" if started else "not started")
    return RedirectResponse("/stats#reprice", status_code=303)


@app.get("/review", response_class=HTMLResponse)
def review(request: Request):
    with db.connect(settings.database_path) as conn:
        thin = db.items_to_review(conn)
        items = db.list_items(conn)
    wide = stats.film_wide(items)
    checks = [(it, why, spine.Box.loads(it["spine_box"])) for it in items
              if (why := shelf.spine_check(it))]
    n_photo = sum(1 for it in items if shelf.spine_face(it)[0] == "photo")
    return TEMPLATES.TemplateResponse(
        request, "review.html",
        {"thin": thin, "checks": checks, "n_photo": n_photo, "wide": wide})


def _price_kinds(items) -> dict[str, int]:
    """How many items' latest price was fetched from eBay, typed in by hand, or is missing."""
    kinds = {"fetched": 0, "by_hand": 0, "unpriced": 0}
    for it in items:
        if it["latest_median"] is None:
            kinds["unpriced"] += 1
        elif it["latest_source"] == MANUAL_SOLD:
            kinds["by_hand"] += 1
        else:
            kinds["fetched"] += 1
    return kinds


def _basis(items) -> str:
    """What the shelf total is made of: asks, sold prices, or both."""
    sold = {it["latest_source"] in SOLD_SOURCES for it in items
            if it["latest_median"] is not None}
    return {frozenset({False}): "asks", frozenset({True}): "sold prices"}.get(
        frozenset(sold), "asks and sold prices" if sold else "no prices yet")


def _shelf_floor(items) -> list[dict]:
    """Sum of what each item is worth, one total per currency, largest first.

    Each total splits into what sold prices and asks make of it (`sold`, `asks`).
    Beside it, what was paid: `paid` over the items with a paid price, and `gain`
    (worth minus paid) over the ones that have both a price and a paid price,
    with `n_gain` saying how many that is. A paid price is in its item's valuation
    currency; an unpriced item's counts toward the largest total's.
    """
    totals: dict[str, dict] = {}
    for it in items:
        if it["latest_median"] is None:
            continue
        t = totals.setdefault(it["latest_currency"], {
            "currency": it["latest_currency"], "total": 0.0, "n": 0, "sold": 0.0,
            "n_sold": 0, "asks": 0.0, "paid": 0.0, "n_paid": 0, "gain": 0.0, "n_gain": 0})
        t["total"] += it["latest_median"]
        t["n"] += 1
        if it["latest_source"] in SOLD_SOURCES:
            t["sold"] += it["latest_median"]
            t["n_sold"] += 1
        else:
            t["asks"] += it["latest_median"]
        if it["paid_price"] is not None:
            t["gain"] += it["latest_median"] - it["paid_price"]
            t["n_gain"] += 1
    ranked = sorted(totals.values(), key=lambda t: -t["n"])
    for it in items:
        if it["paid_price"] is None or not ranked:
            continue
        t = totals.get(it["latest_currency"]) or ranked[0]
        t["paid"] += it["paid_price"]
        t["n_paid"] += 1
    return ranked


def _add_page(
    request, values=None, errors=(), draft=None, doubts=(), via="", status_code=200
):
    ctx = {
        "values": values or {},
        "errors": list(errors),
        "doubts": list(doubts),
        "via": via,
        "draft": draft,
        "held": [k for k, _, _ in photos.load_draft(settings.photo_dir, draft) or []]
        if draft else [],
    }
    return TEMPLATES.TemplateResponse(request, "add.html", ctx, status_code=status_code)


async def _read_uploads(files) -> tuple[list[tuple[str, str, bytes]], list[str]]:
    """(kind, ext, data) for each file the form sent, and an error per bad one."""
    uploads, errors = [], []
    for kind, f in zip(photos.KINDS, files):
        if f is None or not f.filename:
            continue
        data = await f.read()
        try:
            uploads.append((kind, photos.check(kind, f.content_type, data), data))
        except photos.PhotoError as exc:
            errors.append(str(exc))
    return uploads, errors


def _item_values(
    title, format, edition, retailer, region, upc, condition, notes, paid_price="", paid_on="",
    genre="", director="", year="", edition_keywords="", search_query="",
) -> dict:
    return {
        "title": _clean(title), "format": _clean(format), "edition": _clean(edition),
        "retailer": _clean(retailer), "region": _clean(region), "upc": _clean(upc),
        "condition": _clean(condition), "notes": _clean(notes),
        "paid_price": _clean(paid_price), "paid_on": _clean(paid_on),
        "genre": _clean(genre), "director": _clean(director), "year": _clean(year),
        "edition_keywords": _clean(edition_keywords), "search_query": _clean(search_query),
    }


def _item_errors(values: dict) -> list[str]:
    """Errors in the typed fields; a good paid price is turned into a number here."""
    errors = []
    if not values["title"]:
        errors.append("title is required")
    if values["upc"] and not values["upc"].isdigit():
        errors.append("UPC is digits only")
    year = values["year"]
    if year is not None:
        if str(year).isdigit() and 1880 <= int(year) <= 2100:
            values["year"] = int(year)
        else:
            errors.append("year is four digits, like 1979")
    paid = values["paid_price"]
    if paid is not None:
        try:
            values["paid_price"] = round(float(str(paid).lstrip("$").replace(",", "")), 2)
        except ValueError:
            errors.append("paid price must be a number")
        else:
            if values["paid_price"] < 0:
                errors.append("paid price cannot be negative")
    return errors


@app.get("/add", response_class=HTMLResponse)
def add_form(request: Request, _: str = Depends(require_admin)):
    return _add_page(request)


@app.post("/add/identify")
async def identify(
    request: Request,
    _: str = Depends(require_admin),
    identifier: Identifier = Depends(get_identifier),
    front: UploadFile | None = File(None),
    spine: UploadFile | None = File(None),
    back: UploadFile | None = File(None),
    other: UploadFile | None = File(None),
    boxed: str = Form(""),
):
    uploads, errors = await _read_uploads((front, spine, back, other))
    if not errors and not any(k == "front" for k, _, _ in uploads):
        errors.append("a front photo is required to identify")
    if errors:
        return _add_page(request, errors=errors, status_code=422)

    draft = photos.save_draft(settings.photo_dir, uploads)
    shots = [(k, data) for k, _, data in uploads]
    code = await run_in_threadpool(barcode.decode_any, shots)
    try:
        # The Claude call blocks for tens of seconds; keep it off the event loop.
        found = await run_in_threadpool(identifier.identify, shots, boxed=bool(boxed))
    except IdentifyError as exc:
        log.warning("identify failed: %s", exc)
        values = {"upc": code} if code else None
        return _add_page(
            request, values=values, errors=[f"identify failed: {exc}"], draft=draft,
            status_code=502,
        )
    found = barcode.apply(found, code)
    return _add_page(
        request, values=found.values, draft=draft, doubts=found.doubts, via=found.via
    )


@app.post("/items")
async def create_item(
    request: Request,
    _: str = Depends(require_admin),
    title: str = Form(""),
    format: str = Form(""),
    edition: str = Form(""),
    retailer: str = Form(""),
    region: str = Form(""),
    upc: str = Form(""),
    condition: str = Form(""),
    notes: str = Form(""),
    paid_price: str = Form(""),
    paid_on: str = Form(""),
    genre: str = Form(""),
    director: str = Form(""),
    year: str = Form(""),
    edition_keywords: str = Form(""),
    search_query: str = Form(""),
    draft: str = Form(""),
    front: UploadFile | None = File(None),
    spine: UploadFile | None = File(None),
    back: UploadFile | None = File(None),
    other: UploadFile | None = File(None),
):
    values = _item_values(title, format, edition, retailer, region, upc, condition, notes,
                          paid_price, paid_on, genre, director, year, edition_keywords,
                          search_query)
    errors = _item_errors(values)

    # Validate every upload before anything touches disk or the database.
    uploads, upload_errors = await _read_uploads((front, spine, back, other))
    errors += upload_errors
    draft = draft.strip() or None
    if draft:
        held = photos.load_draft(settings.photo_dir, draft)
        if held is None:
            errors.append("the photos from identify have expired — take them again")
            draft = None
        else:
            # A file chosen on the review page replaces the held photo of that kind.
            sent = {k for k, _, _ in uploads}
            uploads += [h for h in held if h[0] not in sent]
    if not any(k == "front" for k, _, _ in uploads):
        errors.append("a front photo is required")

    if errors:
        # The browser drops file inputs on a re-render; a draft keeps its photos.
        return _add_page(request, values=values, errors=errors, draft=draft, status_code=422)

    uploads.sort(key=lambda u: photos.KINDS.index(u[0]))
    front = next(data for kind, _, data in uploads if kind == "front")
    color = await run_in_threadpool(photos.spine_color, front)
    if not values["upc"]:  # saved without identify: the barcode, if the photos show one
        values["upc"] = await run_in_threadpool(
            barcode.decode_any, [(k, data) for k, _, data in uploads]
        )
    conn = db.connect(settings.database_path)
    item_id = None
    saved: list[tuple[int, str, bytes]] = []
    try:
        item_id = db.insert_item(conn, values)
        if values["genre"] or values["director"]:
            db.set_film_by_hand(conn, item_id)  # typed on the add form: TMDB stays out
        for kind, ext, data in uploads:
            rel = photos.save(settings.photo_dir, item_id, kind, ext, data)
            saved.append((db.insert_photo(conn, item_id, kind, rel), kind, data))
        db.set_spine_color(conn, item_id, color)
        conn.commit()
    except Exception:
        conn.rollback()
        if item_id is not None:
            photos.remove_item_dir(settings.photo_dir, item_id)
        raise
    finally:
        conn.close()
    if draft:
        photos.discard_draft(settings.photo_dir, draft)
    await run_in_threadpool(write_derived_for, item_id, saved)
    log.info("item %d saved with %d photos", item_id, len(uploads))
    look_up_films()
    return RedirectResponse(f"/items/{item_id}", status_code=303)


@app.get("/items/{item_id}", response_class=HTMLResponse)
def item_page(request: Request, item_id: int, error: str | None = None):
    with db.connect(settings.database_path) as conn:
        item = db.get_item(conn, item_id)
        if item is None:
            raise HTTPException(404, "no such item")
        valuations = db.item_valuations(conn, item_id)
        # The price shown is the one the shelf counts; the listings are the ones behind
        # it, or with no price, behind the last fetch that found any.
        shown = current_worth(valuations)
        listed = shown or next((v for v in valuations if v["n_listings"]), None)
        listings = db.valuation_listings(conn, listed["id"]) if listed else []
        missed = reprice.item_note(conn, item_id)
        status = reprice_status(conn, request.app.state.scheduler)
        ctx = {
            "missed": missed,
            "reprice": status,
            "item": item,
            "photos": db.item_photos(conn, item_id),
            "valuations": valuations,
            "listings": listings,
            "error": error,
            "asks_via_serpapi": asks_via_serpapi(),
            "item_opened": is_opened(item),
            "item_sealed": bool((item["condition"] or "").strip()) and not is_opened(item),
            "sold_lookup": settings.sold_lookup_enabled,
            "gain": _gain(item, shown),
            "latest": shown,
            "newer_ask": shown is not None and valuations[0]["id"] != shown["id"]
            and valuations[0]["median"] is not None and valuations[0],
            "sold_until": _sold_until(shown),
            "sold": bool(shown) and shown["source"] in SOLD_SOURCES,
            "sold_fit": _sold_fit(item, shown, listings),
            "confidence": shown and confidence(
                shown["n_listings"], shown["low"], shown["high"], shown["median"],
                "sale" if shown["source"] in SOLD_SOURCES else "listing", shown["matched"]),
            "chart": present.line([(v["fetched_at"], v["median"]) for v in reversed(valuations)
                                   if v["median"] is not None]),
        }
    return TEMPLATES.TemplateResponse(request, "item.html", ctx)


def _sold_until(latest) -> str | None:
    """The day a sold price stops outranking newer asks; None for an ask."""
    if not latest or latest["source"] not in SOLD_SOURCES:
        return None
    until = datetime.fromisoformat(latest["fetched_at"]) + SOLD_FRESH
    return present.day(until.strftime("%Y-%m-%d %H:%M:%S"))


def _sold_fit(item, latest, listings) -> bool | None:
    """Were the sales counted in a sold price all copies like the item (used for an
    opened one, new for a sealed one)? None for an ask, a price typed in, or an item
    with no condition, where the page says nothing about it."""
    if not latest or latest["source"] not in SOLD_SOURCES:
        return None
    fits = [fits_condition(item, ls["condition"]) for ls in listings if not ls["excluded"]]
    if not fits or None in fits:
        return None
    return all(fits)


def _gain(item, latest) -> float | None:
    """Worth minus paid, when the item has both; None otherwise."""
    if item["paid_price"] is None or latest is None or latest["median"] is None:
        return None
    return round(latest["median"] - item["paid_price"], 2)


@app.get("/items/{item_id}/edit", response_class=HTMLResponse)
def edit_form(request: Request, item_id: int, _: str = Depends(require_admin)):
    with db.connect(settings.database_path) as conn:
        item = db.get_item(conn, item_id)
    if item is None:
        raise HTTPException(404, "no such item")
    ctx = {"item": item, "values": dict(item), "errors": [], "editing": True}
    return TEMPLATES.TemplateResponse(request, "edit.html", ctx)


@app.post("/items/{item_id}/identify", response_class=HTMLResponse)
async def reidentify(
    request: Request,
    item_id: int,
    _: str = Depends(require_admin),
    identifier: Identifier = Depends(get_identifier),
):
    """Identify a saved item again from its stored photos; nothing is saved here.

    The suggestions fill the edit form over the current values (a field the model
    leaves blank keeps what is there), with each change listed for review.
    """
    with db.connect(settings.database_path) as conn:
        item = db.get_item(conn, item_id)
        if item is None:
            raise HTTPException(404, "no such item")
        rows = db.item_photos(conn, item_id)
    stored = [(r["kind"], photos.resolve(settings.photo_dir, r["path"])) for r in rows]
    shots = [(kind, path.read_bytes()) for kind, path in stored if path]
    current = dict(item)
    ctx = {"item": item, "values": current, "errors": [], "editing": True}
    if not any(kind == "front" for kind, _ in shots):
        ctx["errors"] = ["no front photo on disk to identify from"]
        return TEMPLATES.TemplateResponse(request, "edit.html", ctx, status_code=422)
    # The box tick is not stored; an edition that already names one stands in for it.
    boxed = bool(BOXED_WORDS.search(item["edition"] or ""))
    code = await run_in_threadpool(barcode.decode_any, shots)
    try:
        found = await run_in_threadpool(identifier.identify, shots, boxed=boxed)
    except IdentifyError as exc:
        log.warning("re-identify of item %d failed: %s", item_id, exc)
        ctx["errors"] = [f"identify failed: {exc}"]
        return TEMPLATES.TemplateResponse(request, "edit.html", ctx, status_code=502)
    found = barcode.apply(found, code)
    values = {**current, **{f: v for f, v in found.values.items() if v}}
    ctx["values"] = values
    ctx["doubts"] = found.doubts
    ctx["via"] = found.via
    ctx["changes"] = [(f, current[f], values[f]) for f in FIELDS if values[f] != current[f]]
    ctx["identified"] = True
    log.info("item %d re-identified: %d fields differ", item_id, len(ctx["changes"]))
    return TEMPLATES.TemplateResponse(request, "edit.html", ctx)


def _film_edit(conn, item, values: dict) -> bool:
    """What an edit does to the item's TMDB state; True when it needs a lookup.

    Genre or director changed here: set by hand, and TMDB leaves the item alone for
    good. Both cleared: handed back to TMDB. Otherwise a new title may be a
    different film, so what the old one's lookup filled goes and TMDB fills it
    again — unless the item was set by hand. Only the last two need TMDB on.
    """
    fields = ("genre", "director")
    had = any(item[f] for f in fields)
    if had and not any(values[f] for f in fields):
        db.reset_film(conn, item["id"])
        return get_films() is not None
    if any(values[f] != item[f] for f in fields):
        db.set_film_by_hand(conn, item["id"])
        return False
    if (values["title"] != item["title"] and item["tmdb_id"] != db.HAND_SET
            and get_films() is not None):
        values.update(genre=None, director=None)
        db.reset_film(conn, item["id"])
        return True
    return False


@app.post("/items/{item_id}")
def update_item(
    request: Request,
    item_id: int,
    _: str = Depends(require_admin),
    title: str = Form(""),
    format: str = Form(""),
    edition: str = Form(""),
    retailer: str = Form(""),
    region: str = Form(""),
    upc: str = Form(""),
    condition: str = Form(""),
    notes: str = Form(""),
    paid_price: str = Form(""),
    paid_on: str = Form(""),
    genre: str = Form(""),
    director: str = Form(""),
    year: str = Form(""),
    edition_keywords: str = Form(""),
    search_query: str = Form(""),
):
    values = _item_values(title, format, edition, retailer, region, upc, condition, notes,
                          paid_price, paid_on, genre, director, year, edition_keywords,
                          search_query)
    errors = _item_errors(values)
    with db.connect(settings.database_path) as conn:
        item = db.get_item(conn, item_id)
        if item is None:
            raise HTTPException(404, "no such item")
        if errors:
            ctx = {"item": item, "values": values, "errors": errors, "editing": True}
            return TEMPLATES.TemplateResponse(request, "edit.html", ctx, status_code=422)
        look_up = _film_edit(conn, item, values)
        db.update_item(conn, item_id, values)
    log.info("item %d edited", item_id)
    if look_up:
        look_up_films()
    return RedirectResponse(f"/items/{item_id}", status_code=303)


@app.post("/items/{item_id}/value")
def value(
    item_id: int, _: str = Depends(require_admin), source: PricingSource = Depends(get_pricing)
):
    # A plain def: FastAPI runs it in the threadpool, so the eBay call does not block the loop.
    with db.connect(settings.database_path) as conn:
        if db.get_item(conn, item_id) is None:
            raise HTTPException(404, "no such item")
        try:
            value_item(conn, item_id, source)
        except PricingError as exc:
            log.warning("valuation of item %d failed: %s", item_id, exc)
            query = urlencode({"error": str(exc)})
            return RedirectResponse(f"/items/{item_id}?{query}", status_code=303)
    return RedirectResponse(f"/items/{item_id}", status_code=303)


@app.post("/items/{item_id}/sold")
def sold(item_id: int, prices: str = Form(""), _: str = Depends(require_admin)):
    with db.connect(settings.database_path) as conn:
        if db.get_item(conn, item_id) is None:
            raise HTTPException(404, "no such item")
        try:
            record_sold(conn, item_id, parse_prices(prices))
        except PricingError as exc:
            query = urlencode({"error": str(exc)})
            return RedirectResponse(f"/items/{item_id}?{query}", status_code=303)
    log.info("item %d: sold prices entered", item_id)
    return RedirectResponse(f"/items/{item_id}", status_code=303)


@app.post("/items/{item_id}/listings/{listing_id}/toggle")
def listing_toggle(item_id: int, listing_id: int, _: str = Depends(require_admin)):
    with db.connect(settings.database_path) as conn:
        if db.get_item(conn, item_id) is None:
            raise HTTPException(404, "no such item")
        if toggle_listing(conn, item_id, listing_id) is None:
            raise HTTPException(404, "no such listing on this item")
    log.info("item %d: listing %d toggled", item_id, listing_id)
    return RedirectResponse(f"/items/{item_id}#listings", status_code=303)


@app.post("/items/{item_id}/sold/lookup")
def sold_lookup(
    item_id: int,
    _: str = Depends(require_admin),
    source: PricingSource = Depends(get_sold_pricing),
):
    if not settings.sold_lookup_enabled:
        raise HTTPException(404, "sold lookup is disabled")
    return value(item_id, _, source)


@app.get("/photos/{photo_id}")
def photo(photo_id: int):
    with db.connect(settings.database_path) as conn:
        row = db.get_photo(conn, photo_id)
    path = photos.resolve(settings.photo_dir, row["path"]) if row else None
    if path is None:
        raise HTTPException(404, "no such photo")
    return FileResponse(path)


@app.get("/photos/{photo_id}/thumb")
def photo_thumb(photo_id: int):
    """The photo's thumbnail; the original when the photo will not decode."""
    # A plain def: the first request decodes the original, off the event loop.
    with db.connect(settings.database_path) as conn:
        row = db.get_photo(conn, photo_id)
    path = photos.resolve(settings.photo_dir, row["path"]) if row else None
    if path is None:
        raise HTTPException(404, "no such photo")
    return FileResponse(photos.ensure_thumb(settings.photo_dir, photo_id, path) or path)


@app.get("/items/{item_id}/spine.jpg")
def item_spine(item_id: int):
    with db.connect(settings.database_path) as conn:
        row = db.front_photo(conn, item_id)
    path = photos.resolve(settings.photo_dir, row["path"]) if row else None
    strip = photos.ensure_spine(settings.photo_dir, item_id, path) if path else None
    if strip is None:
        raise HTTPException(404, "no spine art for this item")
    return FileResponse(strip, media_type="image/jpeg")


def _spine_source(item_id: int) -> tuple[sqlite3.Row, Path | None]:
    """The item and its spine photo on disk (None if it has none); 404 for no item."""
    with db.connect(settings.database_path) as conn:
        item = db.get_item(conn, item_id)
        row = db.spine_photo(conn, item_id)
    if item is None:
        raise HTTPException(404, "no such item")
    return item, photos.resolve(settings.photo_dir, row["path"]) if row else None


@app.get("/items/{item_id}/spine-photo.jpg")
def item_spine_photo(item_id: int):
    """The spine cut from the spine photo; re-cut from the stored box if the file is gone."""
    dest = photos.spine_photo_path(settings.photo_dir, item_id)
    if not dest.is_file():
        item, path = _spine_source(item_id)
        box = spine.Box.loads(item["spine_box"])
        if box is None or path is None:
            raise HTTPException(404, "no spine photo cut for this item")
        measure_spine(item_id, path.read_bytes(), box)
        if not dest.is_file():
            raise HTTPException(404, "no spine photo cut for this item")
    return FileResponse(dest, media_type="image/jpeg")


FRAME_H = 900  # the crop editor's picture


@app.get("/items/{item_id}/spine-frame.jpg")
def item_spine_frame(item_id: int, turns: int = 0, angle: float = 0.0):
    """The spine photo turned and straightened as a box is measured, for the editor."""
    _, path = _spine_source(item_id)
    img = spine.open_upright(path.read_bytes(), FRAME_H) if path else None
    if img is None:
        raise HTTPException(404, "no spine photo for this item")
    img = spine.frame(img, turns, max(-45.0, min(45.0, angle)))
    img.thumbnail((FRAME_H, FRAME_H))
    return Response(spine.strip_jpeg(img), media_type="image/jpeg")


def _spine_page(request: Request, item, box: spine.Box, status_code: int = 200, found=True):
    face, _ = shelf.spine_face(item)
    ctx = {"item": item, "box": box, "found": found, "face": face,
           "has_strip": bool(item["spine_box"]),
           "auto": "photo" if item["spine_box"] and item["spine_reads"] else "edge"}
    return TEMPLATES.TemplateResponse(request, "spine.html", ctx, status_code=status_code)


@app.get("/items/{item_id}/spine", response_class=HTMLResponse)
def spine_form(request: Request, item_id: int, _: str = Depends(require_admin)):
    item, path = _spine_source(item_id)
    if path is None:
        raise HTTPException(404, "this item has no spine photo")
    box = spine.Box.loads(item["spine_box"])
    return _spine_page(request, item, box or spine.Box(*spine.DEFAULT), found=box is not None)


def _fraction(value: str) -> float:
    """A percentage from the crop form as a fraction of the frame, held inside it."""
    try:
        return min(1.0, max(0.0, float(value) / 100))
    except ValueError:
        return 0.0


SPINE_CHOICES = {"auto": None, "photo": "photo", "edge": "edge"}


@app.post("/items/{item_id}/spine")
def spine_save(
    request: Request,
    item_id: int,
    _: str = Depends(require_admin),
    action: str = Form("save"),
    choice: str = Form("auto"),
    x0: str = Form("0"), y0: str = Form("0"), x1: str = Form("100"), y1: str = Form("100"),
    angle: float = Form(0.0),
    turns: int = Form(0),
    next: str = Form(""),
):
    """choose: only which spine the shelf shows. save: that, and the crop as drawn.
    rotate: a quarter turn and look for the case again. redetect: look again, upright."""
    item, path = _spine_source(item_id)
    if path is None:
        raise HTTPException(404, "this item has no spine photo")
    with db.connect(settings.database_path) as conn:
        db.set_spine_choice(conn, item_id, SPINE_CHOICES.get(choice))
        conn.commit()
    back = next if next in ("/review", f"/items/{item_id}") else f"/items/{item_id}"
    if action == "choose":
        return RedirectResponse(back, status_code=303)
    data = path.read_bytes()
    if action in ("rotate", "redetect"):
        turned = (turns + 1) % 4 if action == "rotate" else 0
        found = spine.detect(data, turned)
        measure_spine(item_id, data, found)
        with db.connect(settings.database_path) as conn:
            item = db.get_item(conn, item_id)
        box = found or spine.Box(*spine.DEFAULT, 0.0, turned)
        return _spine_page(request, item, box, found=found is not None)
    box = spine.Box(_fraction(x0), _fraction(y0), _fraction(x1), _fraction(y1),
                    max(-45.0, min(45.0, angle)), turns % 4)
    if box.x1 - box.x0 < 0.01 or box.y1 - box.y0 < 0.05:
        return _spine_page(request, item, box, status_code=422)
    measure_spine(item_id, data, box)
    log.info("item %d: spine crop saved", item_id)
    return RedirectResponse(back, status_code=303)


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    return FileResponse(STATIC / "favicon.ico", media_type="image/x-icon")


@app.get("/healthz")
def healthz():
    try:
        with db.connect(settings.database_path) as conn:
            n = db.count_items(conn)
    except sqlite3.Error as exc:
        log.error("healthz: database unreachable: %s", exc)
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=503)
    return {"ok": True, "items": n}
