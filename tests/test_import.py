"""CSV import, first slice: a file laid out like the export, uploaded, previewed with
the add form's checks, then saved; repeats of shelf cases left out, no valuations."""

import re
from pathlib import Path

from app import csvio, db, main
from app import settings as settings_module
from tests.conftest import AUTH
from tests.test_csvio import _export, _value
from tests.test_items import post_item

HEADER = ",".join(csvio.COLUMNS)


def rows(sql, *args):
    with db.connect(settings_module.settings.database_path) as conn:
        return conn.execute(sql, args).fetchall()


def upload(client, text: str | bytes, name="shelf.csv", auth=AUTH):
    data = text.encode() if isinstance(text, str) else text
    return client.post("/import", files={"file": (name, data, "text/csv")}, auth=auth,
                       follow_redirects=False)


def preview(client, text, **kw):
    r = upload(client, text, **kw)
    assert r.status_code == 303, r.text
    token = r.headers["location"].rsplit("/", 1)[1]
    page = client.get(f"/import/{token}", auth=AUTH)
    assert page.status_code == 200
    return token, page.text


def do_import(client, text, include=()):
    token, _ = preview(client, text)
    r = client.post(f"/import/{token}", data={"include": [str(n) for n in include]},
                    auth=AUTH)
    assert r.status_code == 200
    return r.text


def sheet(*lines, header="title,edition,retailer,upc,paid_price,year") -> str:
    return "\n".join((header,) + lines) + "\n"


# --- admin only ---------------------------------------------------------------------


def test_import_needs_the_admin_login(admin):
    assert upload(admin, sheet("Alien,,,,,"), auth=None).status_code == 401
    assert admin.get(f"/import/{'0' * 32}").status_code == 401
    assert admin.post(f"/import/{'0' * 32}").status_code == 401
    assert rows("SELECT * FROM items") == []


def test_stats_offers_the_import_even_on_an_empty_shelf(admin):
    page = admin.get("/stats").text
    assert 'action="/import"' in page and 'enctype="multipart/form-data"' in page


# --- upload and preview ---------------------------------------------------------------


def test_preview_saves_nothing(admin):
    _, page = preview(admin, sheet("Alien,Steelbook,Zavvi,,,", "Heat,,,,,"))
    assert "2 to add" in page and "Alien" in page and "Heat" in page
    assert rows("SELECT * FROM items") == []


def test_a_file_without_a_title_column_is_refused(admin):
    r = upload(admin, "Movie,Barcode\nAlien,012345678905\n")
    assert r.status_code == 422
    assert "no title column" in r.text


def test_an_empty_upload_is_refused(admin):
    assert upload(admin, b"").status_code == 422
    r = admin.post("/import", auth=AUTH)
    assert r.status_code == 422 and "choose a CSV file" in r.text


def test_too_many_rows_is_refused(admin, monkeypatch):
    monkeypatch.setattr(csvio, "MAX_ROWS", 3)
    r = upload(admin, sheet(*["Alien,,,,,"] * 4))
    assert r.status_code == 422 and "more than 3 rows" in r.text


def test_an_unknown_or_stale_token_is_404(admin):
    assert admin.get(f"/import/{'0' * 32}", auth=AUTH).status_code == 404
    assert admin.get("/import/..%2F..%2Fetc", auth=AUTH).status_code == 404
    assert admin.post(f"/import/{'0' * 32}", auth=AUTH).status_code == 404


def test_rows_are_checked_by_the_add_forms_rules(admin):
    text = sheet(",Steelbook,,,,", "Heat,,,12ab,,", "Jaws,,,,ten dollars,", "Ran,,,,,19",
                 "Alien,,,,-3,", "Good,,,,\"$1,234.50\",1979")
    _, page = preview(admin, text)
    assert "Line 2: title is required" in page
    assert "Line 3 (Heat): UPC is digits only" in page
    assert "Line 4 (Jaws): paid price must be a number" in page
    assert "Line 5 (Ran): year is four digits" in page
    assert "Line 6 (Alien): paid price cannot be negative" in page
    assert "1 to add" in page and "5 with problems" in page
    html = do_import(admin, text)
    assert "1 case added" in html
    (item,) = rows("SELECT * FROM items")
    assert (item["title"], item["paid_price"], item["year"]) == ("Good", 1234.5, 1979)


def test_an_eleven_digit_upc_gets_its_leading_zero_back(admin):
    # 012345678905 read as a number by a spreadsheet comes back as 12345678905
    _, page = preview(admin, sheet("Alien,,,12345678905,,"))
    assert "12345678905 had lost its leading zero" in page
    do_import(admin, sheet("Alien,,,12345678905,,"))
    assert rows("SELECT upc FROM items")[0]["upc"] == "012345678905"


def test_a_upc_failing_its_check_digit_is_dropped_not_the_row(admin):
    do_import(admin, sheet("Alien,,,012345678901,,"))
    (item,) = rows("SELECT * FROM items")
    assert (item["title"], item["upc"]) == ("Alien", None)


def test_the_exports_formula_apostrophe_comes_off(admin):
    do_import(admin, sheet("'=HYPERLINK(1),'-,'@home,,,"))
    (item,) = rows("SELECT * FROM items")
    assert (item["title"], item["edition"], item["retailer"]) == ("=HYPERLINK(1)", "-", "@home")


def test_semicolons_tabs_and_windows_1252_are_read(admin):
    do_import(admin, "title;edition;retailer\nAmélie;Steelbook;Fnac\n".encode("cp1252"))
    do_import(admin, "title\tedition\nHeat\tSteelbook\n")
    do_import(admin, "﻿title,retailer\nLéon,Zavvi\n")
    got = sorted((r["title"], r["edition"], r["retailer"]) for r in rows("SELECT * FROM items"))
    assert got == [("Amélie", "Steelbook", "Fnac"), ("Heat", "Steelbook", None),
                   ("Léon", None, "Zavvi")]


def test_typed_genre_or_director_is_set_by_hand(admin):
    do_import(admin, sheet("Alien,Ridley Scott", "Heat,", header="title,director"))
    by_title = {r["title"]: r["tmdb_id"] for r in rows("SELECT title, tmdb_id FROM items")}
    assert by_title == {"Alien": db.HAND_SET, "Heat": None}


# --- repeats ---------------------------------------------------------------------------


def test_a_case_already_on_the_shelf_is_flagged_and_left_out(admin):
    post_item(admin, data={"title": "Alien", "upc": "012345678905"})
    post_item(admin, data={"title": "Heat", "upc": "", "edition": "Steelbook",
                           "retailer": "Zavvi"})
    text = sheet("Alien (other name),,,012345678905,,", "heat,steelbook,ZAVVI,,,",
                 "Jaws,,,,,")
    _, page = preview(admin, text)
    assert "1 to add" in page and "2 already on the shelf" in page
    assert "same UPC as" in page and "same title, edition and retailer as" in page
    assert 'name="include" value="2"' in page
    do_import(admin, text)
    assert sorted(r["title"] for r in rows("SELECT title FROM items")) == ["Alien", "Heat", "Jaws"]


def test_a_ticked_repeat_is_imported(admin):
    post_item(admin, data={"title": "Alien", "upc": "012345678905"})
    html = do_import(admin, sheet("Alien,,,012345678905,,"), include=[2])
    assert "1 case added" in html and "1 of them ticked" in html
    assert [r["title"] for r in rows("SELECT title FROM items")] == ["Alien", "Alien"]


def test_reimporting_an_export_adds_nothing(admin):
    for title in ("Alien", "Heat"):
        post_item(admin, data={"title": title})
    text = admin.get("/export.csv", auth=AUTH).text
    html = do_import(admin, text)
    assert "0 cases added" in html
    assert len(rows("SELECT * FROM items")) == 2


# --- what an import never does -------------------------------------------------------


def test_a_spreadsheet_price_is_never_saved_as_a_valuation(admin):
    post_item(admin, data={"title": "Alien"})
    with db.connect(settings_module.settings.database_path) as conn:
        _value(conn, 1, "ebay_keyword", 30.0)
    text = admin.get("/export.csv", auth=AUTH).text
    with db.connect(settings_module.settings.database_path) as conn:
        conn.execute("DELETE FROM items")  # cascades the valuation
    html = do_import(admin, text)
    assert "1 case added" in html
    assert "worth" in html  # the ignored worth columns are named on the page
    assert rows("SELECT * FROM valuations") == []
    assert rows("SELECT paid_price FROM items")[0]["paid_price"] is None


def test_worth_column_alone_is_not_a_paid_price(admin):
    do_import(admin, sheet("Alien,45.00", header="title,worth"))
    (item,) = rows("SELECT * FROM items")
    assert item["paid_price"] is None
    assert rows("SELECT * FROM valuations") == []


def test_import_makes_no_third_party_call(admin, monkeypatch):
    def refuse(*a, **k):
        raise AssertionError("import must not price or identify")
    monkeypatch.setattr(main, "get_pricing", refuse)
    monkeypatch.setattr(main, "get_identifier", refuse)
    do_import(admin, sheet("Alien,,,,,"))
    assert len(rows("SELECT * FROM items")) == 1


def test_the_held_file_goes_after_the_import(admin):
    token, _ = preview(admin, sheet("Alien,,,,,"))
    held = Path(settings_module.settings.photo_dir) / main.IMPORT_DIR / f"{token}.csv"
    assert held.is_file()
    admin.post(f"/import/{token}", auth=AUTH)
    assert not held.exists()
    assert admin.post(f"/import/{token}", auth=AUTH).status_code == 404  # not twice


# --- round trip ------------------------------------------------------------------------


def test_export_then_import_into_an_empty_shelf_gives_the_same_cases(admin, tmp_path,
                                                                     monkeypatch):
    post_item(admin, data={
        "title": "Amélie", "year": "2001", "format": "4K UHD", "edition": "Steelbook",
        "retailer": "Zavvi", "region": "UK", "upc": "012345678905", "condition": "sealed",
        "notes": "dent, top left\nsecond line", "genre": "Comedy",
        "director": "Jean-Pierre Jeunet", "paid_price": "$1,234.5", "paid_on": "2024-02-24",
        "edition_keywords": "zavvi, numbered", "search_query": "amelie zavvi steelbook"})
    post_item(admin, data={"title": "=SUM(1)", "upc": "", "notes": "+1 dent",
                           "edition": "-", "retailer": "@home"})
    post_item(admin, data={"title": "Heat", "upc": "", "paid_price": "0"})
    with db.connect(settings_module.settings.database_path) as conn:
        _value(conn, 1, "ebay_keyword", 30.0)
    before = _export(admin)
    text = admin.get("/export.csv", auth=AUTH).text

    # A new, empty shelf.
    monkeypatch.setattr(settings_module.settings, "database_path", str(tmp_path / "new.db"))
    db.init_db(settings_module.settings.database_path)
    assert rows("SELECT * FROM items") == []
    html = do_import(admin, text)
    assert "3 cases added" in html
    after = _export(admin)

    same = [c for c in csvio.ITEM_COLUMNS if c != "id"]
    assert [{c: r[c] for c in same} for r in after] == [{c: r[c] for c in same}
                                                         for r in before]
    assert after[0]["worth"] == ""  # the price did not come across: it is re-fetched


def test_the_preview_escapes_the_cells_it_echoes(admin):
    _, page = preview(admin, sheet("<script>x</script>,,,,,"))
    assert "<script>x</script>" not in page
    assert re.search(r"&lt;script&gt;x&lt;/script&gt;", page)


def test_a_decimal_comma_is_read_as_a_point(admin):
    text = 'title;paid_price\nZavvi one;"12,50"\nBig one;"1,250"\nEuro one;"€ 9,5"\n'
    _, page = preview(admin, text)
    assert "12,50 read as 12.50, a decimal comma" in page
    # the add form takes only a dollar sign, so the euro row is refused as it would be there
    assert "Line 4 (Euro one): paid price must be a number" in page
    do_import(admin, text)
    paid = {r["title"]: r["paid_price"] for r in rows("SELECT title, paid_price FROM items")}
    assert paid == {"Zavvi one": 12.5, "Big one": 1250.0}  # three digits: thousands
