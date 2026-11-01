"""Bulk capture: Queue and next, the drafts queue, /drafts, and what each failure does.

Identification is a stub, as in test_identify.py: nothing here reaches a model or the
network. The queue's thread is replaced by a recorder unless a test runs it itself.
"""

import json
import os
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import db, main, photos
from app import settings as settings_module
from app.identify import Identification
from tests.conftest import AUTH
from tests.test_identify import FOUND, StubIdentifier, draft_token, jpeg


def rows(sql, *args):
    with db.connect(settings_module.settings.database_path) as conn:
        return conn.execute(sql, args).fetchall()


def draft_dir(token: str) -> Path:
    return Path(settings_module.settings.photo_dir) / photos.DRAFT_DIR / token


@pytest.fixture
def woken(monkeypatch):
    """The queue's wake-ups, recorded instead of starting the thread."""
    calls = []
    monkeypatch.setattr(main, "identify_queue", lambda: calls.append(1))
    return calls


@pytest.fixture
def stub_identify():
    """POST /add/identify answered by a stub, as test_identify.py's fixture does."""
    def use(identifier):
        main.app.dependency_overrides[main.get_identifier] = lambda: identifier
        return identifier

    yield use
    main.app.dependency_overrides.pop(main.get_identifier, None)


@pytest.fixture(autouse=True)
def no_pricing(monkeypatch):
    """The queue never prices: any pricing call fails the test."""
    def refuse():
        raise AssertionError("the drafts queue must not price")
    monkeypatch.setattr(main, "get_pricing", refuse)


def queue(client, files=None, boxed=False, auth=AUTH):
    files = files if files is not None else {"front": ("f.jpg", jpeg(10, 10), "image/jpeg")}
    data = {"boxed": "1"} if boxed else {}
    return client.post("/drafts", files=files, data=data, auth=auth, follow_redirects=False)


def queued_token() -> str:
    (row,) = rows("SELECT token FROM drafts ORDER BY created_at DESC, rowid DESC LIMIT 1")
    return row["token"]


# --- Queue and next ---------------------------------------------------------------


def test_queue_needs_credentials(admin, woken):
    assert queue(admin, auth=None).status_code == 401
    assert rows("SELECT * FROM drafts") == []


def test_the_add_form_offers_queue_and_next(admin):
    page = admin.get("/add", auth=AUTH).text
    assert 'formaction="/drafts"' in page and "Queue and next" in page


def test_queue_holds_the_photos_and_answers_at_once(admin, woken):
    front, back = jpeg(10, 10), jpeg(20, 20)
    r = queue(admin, {"front": ("f.jpg", front, "image/jpeg"),
                      "back": ("b.jpg", back, "image/jpeg")}, boxed=True)
    assert r.status_code == 303
    assert r.headers["location"] == "/add?queued=1"
    (row,) = rows("SELECT * FROM drafts")
    assert (row["status"], row["boxed"], row["attempts"], row["found"]) == ("queued", 1, 0, None)
    held = photos.load_draft(settings_module.settings.photo_dir, row["token"])
    assert [(k, d) for k, _, d in held] == [("front", front), ("back", back)]
    assert woken == [1]  # the queue was woken; identify did not run in the request
    assert rows("SELECT * FROM items") == []


def test_the_empty_form_counts_the_queue(admin, woken):
    queue(admin)
    queue(admin)
    page = admin.get("/add?queued=1", auth=AUTH).text
    assert "Queued. Next case." in page
    assert "2 queued · 0 ready" in page
    assert 'name="draft"' not in page  # an empty form, not the last case's


def test_queue_refuses_a_case_without_a_front_photo(admin, woken):
    r = queue(admin, {"back": ("b.jpg", jpeg(), "image/jpeg")})
    assert r.status_code == 422
    assert "a front photo is required to queue a case" in r.text
    assert rows("SELECT * FROM drafts") == []
    assert not (Path(settings_module.settings.photo_dir) / photos.DRAFT_DIR).exists() or \
        not any((Path(settings_module.settings.photo_dir) / photos.DRAFT_DIR).iterdir())
    assert woken == []


def test_queue_refuses_a_file_that_is_not_a_photo(admin, woken):
    r = queue(admin, {"front": ("f.txt", b"hello", "text/plain")})
    assert r.status_code == 422
    assert rows("SELECT * FROM drafts") == []


# --- the queue ----------------------------------------------------------------------


def test_the_queue_identifies_oldest_first_one_at_a_time(admin, woken):
    queue(admin, boxed=True)
    first = queued_token()
    queue(admin)
    second = queued_token()

    seen = []

    class Recording(StubIdentifier):
        def identify(self, shots, boxed=False):
            # one at a time: when one case is being identified no other is
            seen.append([r["status"] for r in rows("SELECT status FROM drafts ORDER BY rowid")])
            return super().identify(shots, boxed)

    stub = Recording(FOUND)
    assert main.drain_queue(stub) == 2
    assert seen == [["identifying", "queued"], ["ready", "identifying"]]
    assert stub.boxed is False  # the second case had no tick

    a, b = (rows("SELECT * FROM drafts WHERE token = ?", t)[0] for t in (first, second))
    assert (a["status"], a["attempts"], b["status"]) == ("ready", 1, "ready")
    found = json.loads(a["found"])
    assert found["values"]["title"] == "Alien"
    assert found["doubts"] == ["retailer: sticker partly peeled"]
    assert a["identified_at"] is not None
    assert main.drain_queue(stub) == 0  # nothing left


def test_the_queue_passes_the_box_tick(admin, woken):
    queue(admin, boxed=True)
    stub = StubIdentifier(FOUND)
    main.drain_queue(stub)
    assert stub.boxed is True


def test_the_queue_takes_the_decoded_barcode_over_the_models(admin, woken, monkeypatch):
    monkeypatch.setattr(main.barcode, "decode_any", lambda shots: "012345678905")
    queue(admin)
    main.drain_queue(StubIdentifier(FOUND))
    (row,) = rows("SELECT * FROM drafts")
    assert row["upc"] == "012345678905"
    found = json.loads(row["found"])
    assert found["values"]["upc"] == "012345678905"
    assert found["doubts"][0].startswith("upc: the barcode decodes to 012345678905")


def test_identify_failure_marks_the_draft_failed_with_the_reason(admin, woken):
    queue(admin)
    draft_id = queued_token()
    main.drain_queue(StubIdentifier(error="no ANTHROPIC_API_KEY set"))
    (row,) = rows("SELECT * FROM drafts")
    assert (row["status"], row["error"], row["attempts"]) == (
        "failed", "no ANTHROPIC_API_KEY set", 1)

    listing = admin.get("/drafts", auth=AUTH).text
    assert "failed: no ANTHROPIC_API_KEY set" in listing

    # It still opens in the form, photos held, to type by hand and save.
    page = admin.get(f"/add?draft={draft_id}", auth=AUTH)
    assert page.status_code == 200
    assert "identify failed: no ANTHROPIC_API_KEY set" in page.text
    assert "Holding front from identify" in page.text
    r = admin.post("/items", data={"title": "Typed by hand", "draft": draft_id}, auth=AUTH,
                   follow_redirects=False)
    assert r.status_code == 303
    assert [i["title"] for i in rows("SELECT title FROM items")] == ["Typed by hand"]
    assert rows("SELECT * FROM drafts") == []


def test_an_unexpected_error_fails_the_draft_and_the_queue_goes_on(admin, woken):
    queue(admin)
    queue(admin)

    class Boom:
        calls = 0

        def identify(self, shots, boxed=False):
            Boom.calls += 1
            if Boom.calls == 1:
                raise RuntimeError("bug")
            return FOUND

    main.drain_queue(Boom())
    assert [r["status"] for r in rows("SELECT status FROM drafts ORDER BY rowid")] == [
        "failed", "ready"]
    assert rows("SELECT error FROM drafts ORDER BY rowid")[0]["error"] == "unexpected error: bug"


def test_photos_gone_fails_the_draft_and_leaves_only_discard(admin, woken):
    queue(admin)
    draft_id = queued_token()
    photos.discard_draft(settings_module.settings.photo_dir, draft_id)  # deleted by hand
    stub = StubIdentifier(FOUND)
    main.drain_queue(stub)
    assert stub.seen is None  # no call made
    (row,) = rows("SELECT * FROM drafts")
    assert (row["status"], row["error"]) == ("failed", "photos gone")

    listing = admin.get("/drafts", auth=AUTH).text
    assert "Photos gone" in listing and f"/add?draft={draft_id}" not in listing
    page = admin.get(f"/add?draft={draft_id}", auth=AUTH)
    assert page.status_code == 410
    assert 'action="/items"' not in page.text
    assert f'action="/drafts/{draft_id}/discard"' in page.text
    assert admin.post(f"/drafts/{draft_id}/discard", auth=AUTH,
                      follow_redirects=False).status_code == 303
    assert rows("SELECT * FROM drafts") == []


def test_restart_mid_queue_puts_the_case_back(admin, woken):
    queue(admin)
    queue(admin)
    with db.connect(settings_module.settings.database_path) as conn:
        db.claim_draft(conn)  # the app stops while this one is being identified
    assert [r["status"] for r in rows("SELECT status FROM drafts ORDER BY rowid")] == [
        "identifying", "queued"]
    woken.clear()
    with TestClient(main.app):  # startup again
        pass
    assert [r["status"] for r in rows("SELECT status FROM drafts ORDER BY rowid")] == [
        "queued", "queued"]
    assert woken  # and the queue runs
    main.drain_queue(StubIdentifier(FOUND))
    attempts = [r["attempts"] for r in rows("SELECT attempts FROM drafts ORDER BY rowid")]
    assert attempts == [2, 1]  # the interrupted call, made again: at most one repeated


def test_startup_with_nothing_queued_starts_no_queue(admin, woken):
    queue(admin)
    main.drain_queue(StubIdentifier(FOUND))
    woken.clear()
    with TestClient(main.app):
        pass
    assert woken == []


def test_the_real_thread_works_the_queue(admin, monkeypatch):
    stub = StubIdentifier(FOUND)
    monkeypatch.setattr(main, "get_identifier", lambda: stub)
    assert queue(admin).status_code == 303
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        if rows("SELECT status FROM drafts")[0]["status"] == "ready":
            break
        time.sleep(0.02)
    assert rows("SELECT status FROM drafts")[0]["status"] == "ready"


# --- at the desk --------------------------------------------------------------------


def test_drafts_needs_credentials(admin):
    assert admin.get("/drafts").status_code == 401
    assert admin.post(f"/drafts/{'0' * 32}/discard").status_code == 401
    assert admin.get(f"/drafts/{'0' * 32}/thumb").status_code == 401


def test_drafts_lists_the_queue_oldest_first(admin, woken):
    queue(admin)
    first = queued_token()
    main.drain_queue(StubIdentifier(Identification(
        {**FOUND.values, "title": "Heat"}, ["a", "b"], via="the Claude API")))
    queue(admin)
    second = queued_token()
    page = admin.get("/drafts", auth=AUTH).text
    assert page.index(first) < page.index(second)
    assert "Heat" in page and "Steelbook" in page
    assert "2 doubts" in page and "by the Claude API" in page
    assert "queued ·" in page  # the second waits
    assert f'href="/add?draft={first}"' in page


def test_a_ready_draft_opens_the_review_form_filled_in(admin, woken):
    queue(admin)
    draft_id = queued_token()
    main.drain_queue(StubIdentifier(Identification(FOUND.values, FOUND.doubts, via="stub")))
    page = admin.get(f"/add?draft={draft_id}", auth=AUTH).text
    assert 'value="Alien"' in page and 'value="Zavvi"' in page
    assert "retailer: sticker partly peeled" in page
    assert "Identified by stub" in page
    assert f'name="draft" value="{draft_id}"' in page
    assert "Back to the queue" in page and f'/drafts/{draft_id}/discard' in page
    assert "Queue and next" not in page


def test_a_queued_draft_not_yet_identified_opens_to_type_by_hand(admin, woken):
    queue(admin)
    page = admin.get(f"/add?draft={queued_token()}", auth=AUTH)
    assert page.status_code == 200
    assert "not identified yet" in page.text


def test_saving_a_draft_makes_the_item_and_drops_the_row(admin, woken):
    front = jpeg(10, 10)
    queue(admin, {"front": ("f.jpg", front, "image/jpeg")})
    draft_id = queued_token()
    main.drain_queue(StubIdentifier(FOUND))
    r = admin.post("/items", data={**FOUND.values, "draft": draft_id}, auth=AUTH,
                   follow_redirects=False)
    assert r.status_code == 303
    (item,) = rows("SELECT * FROM items")
    assert r.headers["location"] == f"/drafts?saved={item['id']}"
    assert (item["title"], item["retailer"]) == ("Alien", "Zavvi")
    (p,) = rows("SELECT path FROM photos")
    assert (Path(settings_module.settings.photo_dir) / p["path"]).read_bytes() == front
    assert rows("SELECT * FROM drafts") == []
    assert not draft_dir(draft_id).exists()
    page = admin.get(r.headers["location"], auth=AUTH).text
    assert f'href="/items/{item["id"]}"' in page and "Nothing queued" in page


def test_a_single_case_save_still_goes_to_the_item(admin, stub_identify):
    stub_identify(StubIdentifier(FOUND))
    r = admin.post("/add/identify", files={"front": ("f.jpg", jpeg(), "image/jpeg")},
                   auth=AUTH)
    r = admin.post("/items", data={**FOUND.values, "draft": draft_token(r.text)}, auth=AUTH,
                   follow_redirects=False)
    assert r.headers["location"].startswith("/items/")


def test_discard_drops_the_row_and_the_photos(admin, woken):
    queue(admin)
    draft_id = queued_token()
    r = admin.post(f"/drafts/{draft_id}/discard", auth=AUTH, follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (303, "/drafts")
    assert rows("SELECT * FROM drafts") == []
    assert not draft_dir(draft_id).exists()
    assert admin.post(f"/drafts/{draft_id}/discard", auth=AUTH).status_code == 404
    assert admin.get(f"/add?draft={draft_id}", auth=AUTH).status_code == 404


def test_the_thumbnail_is_made_from_the_front(admin, woken):
    queue(admin, {"front": ("f.jpg", jpeg(900, 1200), "image/jpeg")})
    draft_id = queued_token()
    r = admin.get(f"/drafts/{draft_id}/thumb", auth=AUTH)
    assert r.status_code == 200 and r.content[:2] == b"\xff\xd8"
    assert (draft_dir(draft_id) / "thumb.jpg").is_file()
    held = photos.load_draft(settings_module.settings.photo_dir, draft_id)
    assert [k for k, _, _ in held] == ["front"]  # the thumbnail is not a photo of the case
    assert admin.get(f"/drafts/{'0' * 32}/thumb", auth=AUTH).status_code == 404
    assert admin.get("/drafts/..%2F..%2Fetc/thumb", auth=AUTH).status_code == 404


def test_a_draft_is_not_an_item(admin, woken):
    queue(admin)
    main.drain_queue(StubIdentifier(FOUND))
    assert admin.get("/healthz").json()["items"] == 0
    assert "Alien" not in admin.get("/").text
    assert "Alien" not in admin.get("/stats").text


# --- pruning ------------------------------------------------------------------------


def test_pruning_spares_queued_drafts(admin, woken, stub_identify):
    queue(admin)
    kept = queued_token()
    single = photos.save_draft(settings_module.settings.photo_dir, [("front", ".jpg", b"x")])
    old = time.time() - 2 * photos.DRAFT_MAX_AGE
    for draft_id in (kept, single):
        os.utime(draft_dir(draft_id), (old, old))
    # A single-case identify prunes stale drafts as it holds its own.
    stub_identify(StubIdentifier(FOUND))
    admin.post("/add/identify", files={"front": ("f.jpg", jpeg(), "image/jpeg")}, auth=AUTH)
    assert draft_dir(kept).is_dir()  # queued: kept until saved or discarded
    assert not draft_dir(single).exists()  # no row: a day, as before


def test_prune_drafts_keeps_the_tokens_it_is_given(tmp_path):
    a = photos.save_draft(str(tmp_path), [("front", ".jpg", b"x")])
    b = photos.save_draft(str(tmp_path), [("front", ".jpg", b"y")])
    photos.prune_drafts(str(tmp_path), max_age=-1, keep={a})
    assert photos.load_draft(str(tmp_path), a) is not None
    assert photos.load_draft(str(tmp_path), b) is None


# --- a case shot twice ----------------------------------------------------------------


def identified(**values):
    return Identification({**FOUND.values, **values}, [], via="stub")


def shelve(**fields) -> int:
    with db.connect(settings_module.settings.database_path) as conn:
        return db.insert_item(conn, fields)


def test_a_draft_matching_an_earlier_draft_is_flagged_by_title_and_edition(admin, woken):
    queue(admin)
    first = queued_token()
    main.drain_queue(StubIdentifier(identified(upc="", retailer="Zavvi")))
    queue(admin)
    second = queued_token()
    # The same film and edition; the retailer read differently does not hide it.
    main.drain_queue(StubIdentifier(identified(upc="", title=" alien ", retailer="")))
    page = admin.get("/drafts", auth=AUTH).text
    assert page.count("Possibly a repeat") == 1  # the later shot, not both
    assert "same title and edition as" in page
    assert f'<a href="/add?draft={first}">Alien</a>, queued earlier' in page
    assert page.index(second) < page.index("Possibly a repeat")


def test_a_draft_matching_a_shelf_case_by_upc_is_flagged(admin, woken):
    item_id = shelve(title="Alien (1979)", upc="036000291452")
    queue(admin)
    main.drain_queue(StubIdentifier(identified()))
    page = admin.get("/drafts", auth=AUTH).text
    assert "Possibly a repeat: same UPC as" in page
    assert f'<a href="/items/{item_id}">Alien (1979)</a>, on the shelf' in page


def test_a_draft_matching_a_shelf_case_by_title_and_edition_is_flagged(admin, woken):
    shelve(title="Alien", edition="STEELBOOK")
    queue(admin)
    main.drain_queue(StubIdentifier(identified(upc="")))
    page = admin.get("/drafts", auth=AUTH).text
    assert "Possibly a repeat: same title and edition as" in page and "on the shelf" in page


def test_a_different_edition_is_not_a_repeat(admin, woken):
    shelve(title="Alien", edition="Steelbook")
    queue(admin)
    main.drain_queue(StubIdentifier(identified(upc="", edition="Mondo Steelbook")))
    queue(admin)
    main.drain_queue(StubIdentifier(identified(upc="", title="Aliens")))
    assert "Possibly a repeat" not in admin.get("/drafts", auth=AUTH).text


def test_drafts_not_yet_identified_are_never_repeats(admin, woken):
    queue(admin)
    queue(admin)
    assert "Possibly a repeat" not in admin.get("/drafts", auth=AUTH).text


def test_a_flagged_repeat_still_saves(admin, woken):
    queue(admin)
    main.drain_queue(StubIdentifier(identified()))
    queue(admin)
    second = queued_token()
    main.drain_queue(StubIdentifier(identified()))
    assert "Possibly a repeat" in admin.get("/drafts", auth=AUTH).text
    r = admin.post("/items", data={**FOUND.values, "draft": second}, auth=AUTH,
                   follow_redirects=False)
    assert r.status_code == 303
    assert len(rows("SELECT * FROM items")) == 1
    # The first draft now repeats the saved case.
    assert "on the shelf" in admin.get("/drafts", auth=AUTH).text
