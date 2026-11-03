"""CSV export: every case, its fields, and what it is worth labelled ask or sold."""

import csv
import io

from app import csvio, db
from app import settings as settings_module

from tests.conftest import AUTH
from tests.test_items import post_item


def _db():
    return db.connect(settings_module.settings.database_path)


def _value(conn, item_id, source, median, n=14, low=None, high=None, matched="judged",
           fetched_at="2026-10-01 12:00:00"):
    conn.execute(
        "INSERT INTO valuations (item_id, source, low, median, high, n_listings, currency,"
        " fetched_at, matched) VALUES (?, ?, ?, ?, ?, ?, 'USD', ?, ?)",
        (item_id, source, low if low is not None else median - 2, median,
         high if high is not None else median + 2, n, fetched_at, matched))


def _read(text: str) -> list[dict]:
    assert text.startswith("﻿")  # Excel reads it as UTF-8
    return list(csv.DictReader(io.StringIO(text[1:])))


def _export(client) -> list[dict]:
    r = client.get("/export.csv", auth=AUTH)
    assert r.status_code == 200
    return _read(r.text)


def test_export_needs_the_admin_login(admin):
    assert admin.get("/export.csv").status_code == 401


def test_export_is_a_csv_download_named_for_the_day(admin):
    r = admin.get("/export.csv", auth=AUTH)
    assert r.headers["content-type"].startswith("text/csv")
    disposition = r.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="steelshelf-')
    assert disposition.endswith('.csv"')


def test_an_empty_shelf_exports_the_header_alone(admin):
    r = admin.get("/export.csv", auth=AUTH)
    assert r.text.lstrip("﻿").splitlines() == [",".join(csvio.COLUMNS)]


def test_every_field_typed_on_the_form_comes_out(admin):
    post_item(admin, data={
        "title": "Amélie", "year": "2001", "format": "4K UHD", "edition": "Steelbook",
        "retailer": "Zavvi", "region": "UK", "upc": "012345678905", "condition": "sealed",
        "notes": "dent, top left", "genre": "Comedy", "director": "Jean-Pierre Jeunet",
        "paid_price": "$1,234.5", "paid_on": "2024-02-24",
        "edition_keywords": "zavvi, numbered", "search_query": "amelie zavvi steelbook"})
    [out] = _export(admin)
    assert out["title"] == "Amélie" and out["year"] == "2001"
    assert out["paid_price"] == "1234.50" and out["paid_on"] == "2024-02-24"
    assert out["notes"] == "dent, top left"  # the comma is quoted, not a new column
    assert out["director"] == "Jean-Pierre Jeunet"
    assert out["upc"] == "012345678905"  # the leading zero stays: it is text
    assert out["edition_keywords"] == "zavvi, numbered"
    assert out["added"]
    assert all(out[c] == "" for c in csvio.WORTH_COLUMNS if c != "added")  # never priced


def test_worth_is_the_price_the_shelf_counts_and_says_ask_or_sold(admin):
    for title in ("Alien", "Heat", "Jaws"):
        post_item(admin, data={"title": title})
    with _db() as conn:
        _value(conn, 1, "ebay_keyword", 30.0, n=14, low=28.0, high=32.0)
        _value(conn, 2, "ebay_keyword", 90.0, fetched_at="2026-09-01 12:00:00")
        _value(conn, 2, "manual_sold", 45.0, n=3, fetched_at="2026-10-02 12:00:00",
               matched="typed")
    alien, heat, jaws = _export(admin)
    assert (alien["worth"], alien["worth_basis"], alien["worth_currency"]) == (
        "30.00", "ask", "USD")
    assert (alien["worth_low"], alien["worth_high"], alien["worth_count"]) == (
        "28.00", "32.00", "14")
    assert alien["worth_confidence"] == "Solid" and alien["worth_source"] == "ebay_keyword"
    assert alien["priced_on"] and alien["worth_matched"] == "judged"
    # The sold price typed in outranks the older ask, and is labelled a sale.
    assert (heat["worth"], heat["worth_basis"], heat["worth_confidence"]) == (
        "45.00", "sold", "Thin")
    assert jaws["worth"] == "" and jaws["worth_basis"] == ""


def test_a_fetch_that_found_nothing_exports_no_worth(admin):
    post_item(admin, data={"title": "Alien"})
    with _db() as conn:
        _value(conn, 1, "ebay_keyword", 30.0, fetched_at="2026-09-01 12:00:00")
        conn.execute("INSERT INTO valuations (item_id, source, n_listings, fetched_at)"
                     " VALUES (1, 'ebay_keyword', 0, '2026-10-01 12:00:00')")
    [out] = _export(admin)
    assert out["worth"] == ""  # the shelf counts it unpriced, so the file does too


def test_cases_come_out_oldest_first(admin):
    for title in ("Alien", "Heat", "Jaws"):
        post_item(admin, data={"title": title})
    assert [r["title"] for r in _export(admin)] == ["Alien", "Heat", "Jaws"]


def test_text_a_spreadsheet_would_run_as_a_formula_is_defused(admin):
    post_item(admin, data={"title": "=HYPERLINK(\"http://x\")", "notes": "+1 dent",
                           "edition": "-", "retailer": "@home"})
    [out] = _export(admin)
    assert out["title"] == "'=HYPERLINK(\"http://x\")"
    assert (out["notes"], out["edition"], out["retailer"]) == ("'+1 dent", "'-", "'@home")


def test_the_export_does_not_touch_the_database(admin):
    post_item(admin, data={"title": "Alien"})
    with _db() as conn:
        _value(conn, 1, "ebay_keyword", 30.0)
        before = [tuple(r) for r in conn.execute("SELECT * FROM valuations")]
    _export(admin)
    with _db() as conn:
        assert [tuple(r) for r in conn.execute("SELECT * FROM valuations")] == before


def test_stats_links_the_export_once_there_is_a_shelf(admin):
    assert 'href="/export.csv"' not in admin.get("/stats").text
    post_item(admin, data={"title": "Alien"})
    assert 'href="/export.csv"' in admin.get("/stats").text


# --- import: guessing columns --------------------------------------------------------


def test_header_names_are_normalised_before_guessing():
    from app.csvio import guess
    assert guess(["  Price-Paid ", "UPC / EAN", "Movie Title", "Name"]) == {
        0: "paid_price", 1: "upc", 2: "title"}


def test_the_first_column_to_claim_a_field_keeps_it():
    from app.csvio import guess
    assert guess(["Movie", "Title", "EAN", "Barcode"]) == {0: "title", 2: "upc"}


def test_id_and_worth_columns_guess_to_nothing():
    from app.csvio import COLUMNS, guess
    mapped = set(guess(list(COLUMNS)).values())
    assert "id" not in mapped and not mapped & {"worth", "worth_low", "added"}
