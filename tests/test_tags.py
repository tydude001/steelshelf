from app import csvio
from app.shelf import tag_list
from tests.conftest import AUTH
from tests.test_items import post_item, rows


def test_tag_list_trims_drops_blanks_and_keeps_each_once():
    assert tag_list(" IMAX, gift ,, imax,  Filmed   for IMAX ") == [
        "IMAX", "gift", "Filmed for IMAX"]
    assert tag_list(None) == [] and tag_list(" , ") == []


def test_the_form_saves_tags_tidied_and_the_case_page_links_each(admin):
    post_item(admin, data={"tags": "IMAX ,, imax, gift"})
    (saved,) = rows("SELECT id, tags FROM items")
    assert saved["tags"] == "IMAX, gift"
    page = admin.get(f"/items/{saved['id']}").text
    assert 'href="/?q=IMAX"' in page and 'href="/?q=gift"' in page
    assert 'name="tags"' in admin.get("/add", auth=AUTH).text


def test_blank_tags_store_null(admin):
    post_item(admin, data={"tags": " , "})
    assert rows("SELECT tags FROM items")[0]["tags"] is None


def test_the_shelf_search_finds_a_tag(admin):
    post_item(admin, data={"title": "The Dark Knight", "upc": "", "tags": "IMAX"})
    post_item(admin, data={"title": "Memento", "upc": ""})
    page = admin.get("/?q=imax").text
    assert "The Dark Knight" in page and "Memento" not in page


def test_tags_round_trip_through_the_csv():
    assert "tags" in csvio.ITEM_COLUMNS and "tags" in csvio.IMPORT_FIELDS
    assert csvio.guess(["Title", "Tags"]) == {0: "title", 1: "tags"}
    assert csvio.guess(["Title", "Labels"]) == {0: "title", 1: "tags"}
