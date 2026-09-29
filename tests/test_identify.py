"""Identification: image prep, UPC check digit, the Claude call (faked), and the routes."""

import io
import json
from pathlib import Path
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from PIL import Image

from app import db, identify, photos
from app import settings as settings_module
from app.identify import ClaudeVision, Identification, IdentifyError
from app.main import app, get_identifier
from tests.conftest import AUTH


def jpeg(w=100, h=60) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), "red").save(out, "JPEG")
    return out.getvalue()


def rows(sql, *args):
    with db.connect(settings_module.settings.database_path) as conn:
        return conn.execute(sql, args).fetchall()


def draft_root() -> Path:
    return Path(settings_module.settings.photo_dir) / photos.DRAFT_DIR


# --- image prep and UPC -------------------------------------------------------


def test_prepare_image_caps_the_long_edge_and_returns_jpeg():
    out = Image.open(io.BytesIO(identify.prepare_image(jpeg(4000, 3000))))
    assert out.format == "JPEG"
    assert max(out.size) == identify.MAX_EDGE


def test_prepare_image_leaves_small_photos_alone():
    out = Image.open(io.BytesIO(identify.prepare_image(jpeg(800, 600))))
    assert out.size == (800, 600)


def test_prepare_image_rejects_non_images():
    with pytest.raises(IdentifyError, match="could not read"):
        identify.prepare_image(b"\xff\xd8\xff\xe0not-really-a-jpeg")


@pytest.mark.parametrize("code", ["036000291452", "4006381333931", "012345678905"])
def test_upc_valid(code):
    assert identify.upc_valid(code)


@pytest.mark.parametrize("code", ["036000291453", "4006381333932", "12345", "03600029145a"])
def test_upc_invalid(code):
    assert not identify.upc_valid(code)


def test_parse_blanks_to_none_and_drops_a_misread_upc():
    found = identify.parse({
        "title": "Alien", "format": "4K UHD", "edition": "Steelbook", "retailer": " ",
        "region": "UK", "upc": "5 053083 261994", "condition": "sealed",
        "doubts": ["region: the ratings logo is partly covered"],
    })
    assert found.values["title"] == "Alien"
    assert found.values["retailer"] is None
    assert found.values["upc"] is None  # check digit fails
    assert found.doubts[0].startswith("upc: read 5053083261994")
    assert found.doubts[1] == "region: the ratings logo is partly covered"


def test_parse_keeps_a_good_upc_as_digits():
    found = identify.parse({"title": "Alien", "upc": "0 36000 29145 2", "doubts": []})
    assert found.values["upc"] == "036000291452"
    assert found.doubts == []


def test_parse_names_the_box_the_owner_ticked():
    # The model cannot reliably see a box from photos of it; the owner's tick decides.
    assert identify.parse({"title": "Alien", "edition": "Steelbook"}, boxed=True
                          ).values["edition"] == "Steelbook (Box set)"
    assert identify.parse({"title": "Alien"}, boxed=True).values["edition"] == "Steelbook (Box set)"
    kept = "Steelbook (Lenticular slip, #12/500)"
    assert identify.parse({"title": "Alien", "edition": kept}, boxed=True).values["edition"] == kept
    assert identify.parse({"title": "Alien", "edition": "Steelbook"}).values["edition"] == "Steelbook"


# --- the Claude call, against a fake client ------------------------------------


class FakeMessages:
    def __init__(self, response=None, error=None):
        self.response, self.error, self.calls = response, error, []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def fake_client(messages):
    return SimpleNamespace(beta=SimpleNamespace(messages=messages))


def response(body, stop_reason="end_turn"):
    text = body if isinstance(body, str) else json.dumps(body)
    return SimpleNamespace(
        stop_reason=stop_reason,
        content=[SimpleNamespace(type="text", text=text)],
        _request_id="req_test",
    )


GOOD = {
    "title": "Alien", "format": "4K UHD", "edition": "Steelbook", "retailer": "Zavvi",
    "region": "UK", "upc": "036000291452", "condition": "sealed", "doubts": [],
}


def test_claude_vision_sends_labelled_photos_and_parses_the_answer():
    msgs = FakeMessages(response(GOOD))
    vision = ClaudeVision("key", "claude-opus-5", client=fake_client(msgs))
    found = vision.identify([("front", jpeg()), ("back", jpeg())])

    assert found.values["retailer"] == "Zavvi"
    (call,) = msgs.calls
    assert call["model"] == "claude-opus-5"
    assert call["fallbacks"] == "default"
    assert call["output_config"]["format"]["schema"] is identify.SCHEMA
    content = call["messages"][0]["content"]
    assert [b["text"] for b in content if b["type"] == "text"][:2] == ["front:", "back:"]
    images = [b for b in content if b["type"] == "image"]
    assert len(images) == 2 and images[0]["source"]["media_type"] == "image/jpeg"


def test_claude_vision_tells_the_model_about_a_box():
    msgs = FakeMessages(response(GOOD))
    vision = ClaudeVision("key", "claude-opus-5", client=fake_client(msgs))
    found = vision.identify([("front", jpeg())], boxed=True)

    assert found.values["edition"] == "Steelbook (Box set)"
    (call,) = msgs.calls
    assert call["messages"][0]["content"][-1]["text"] == identify.BOXED_NOTE


@pytest.mark.parametrize(
    "resp, match",
    [
        (response("", stop_reason="refusal"), "declined"),
        (response("{", stop_reason="max_tokens"), "cut off"),
        (response("not json"), "not valid JSON"),
    ],
)
def test_claude_vision_bad_answers_are_identify_errors(resp, match):
    vision = ClaudeVision("key", "m", client=fake_client(FakeMessages(resp)))
    with pytest.raises(IdentifyError, match=match):
        vision.identify([("front", jpeg())])


def test_claude_vision_offers_capped_web_search():
    msgs = FakeMessages(response(GOOD))
    ClaudeVision("key", "m", client=fake_client(msgs)).identify([("front", jpeg())])

    (call,) = msgs.calls
    (tool,) = call["tools"]
    assert tool["name"] == "web_search" and tool["max_uses"] == 3


def test_claude_vision_reads_the_answer_after_the_searches():
    body = json.dumps(GOOD)
    resp = SimpleNamespace(
        stop_reason="end_turn",
        content=[
            SimpleNamespace(type="text", text="I'll search for this edition."),
            SimpleNamespace(type="server_tool_use"),
            SimpleNamespace(type="web_search_tool_result"),
            # a citation splits the answer over two text blocks
            SimpleNamespace(type="text", text=body[:20]),
            SimpleNamespace(type="text", text=body[20:]),
        ],
        _request_id="req_test",
    )
    vision = ClaudeVision("key", "m", client=fake_client(FakeMessages(resp)))
    assert vision.identify([("front", jpeg())]).values["retailer"] == "Zavvi"


class SequenceMessages:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def test_claude_vision_resumes_a_paused_search_turn():
    paused = response("", stop_reason="pause_turn")
    msgs = SequenceMessages(paused, response(GOOD))
    found = ClaudeVision("key", "m", client=fake_client(msgs)).identify([("front", jpeg())])

    assert found.values["title"] == "Alien"
    first, second = msgs.calls
    assert second["messages"][0] == first["messages"][0]
    assert second["messages"][1] == {"role": "assistant", "content": paused.content}
    assert len(second["messages"]) == 2  # no "continue" user message


def test_claude_vision_gives_up_on_a_turn_that_keeps_pausing():
    paused = response("", stop_reason="pause_turn")
    msgs = SequenceMessages(*[paused] * 4)
    with pytest.raises(IdentifyError, match="ran too long"):
        ClaudeVision("key", "m", client=fake_client(msgs)).identify([("front", jpeg())])
    assert len(msgs.calls) == 4


def test_claude_vision_api_error_is_an_identify_error():
    req = httpx2.Request("POST", "https://api.anthropic.com/v1/messages")
    err = anthropic.InternalServerError(
        "overloaded", response=httpx2.Response(529, request=req), body=None
    )
    vision = ClaudeVision("key", "m", client=fake_client(FakeMessages(error=err)))
    with pytest.raises(IdentifyError, match="HTTP 529"):
        vision.identify([("front", jpeg())])


def test_claude_vision_without_a_key_says_so():
    with pytest.raises(IdentifyError, match="ANTHROPIC_API_KEY"):
        ClaudeVision("", "m").identify([("front", jpeg())])


# --- routes -------------------------------------------------------------------


class StubIdentifier:
    name = "stub"

    def __init__(self, found=None, error=None):
        self.found, self.error, self.seen = found, error, None

    def identify(self, photos, boxed=False):
        self.seen, self.boxed = photos, boxed
        if self.error:
            raise IdentifyError(self.error)
        return self.found


@pytest.fixture
def stub_identifier():
    def use(identifier):
        app.dependency_overrides[get_identifier] = lambda: identifier
        return identifier

    yield use
    app.dependency_overrides.pop(get_identifier, None)


def post_identify(client, files, auth=AUTH):
    return client.post("/add/identify", files=files, auth=auth)


def draft_token(page: str) -> str:
    marker = 'name="draft" value="'
    start = page.index(marker) + len(marker)
    return page[start:start + 32]


FOUND = Identification(
    {"title": "Alien", "format": "4K UHD", "edition": "Steelbook", "retailer": "Zavvi",
     "region": "UK", "upc": "036000291452", "condition": "sealed"},
    ["retailer: sticker partly peeled"],
)


def test_identify_needs_credentials(admin, stub_identifier):
    stub_identifier(StubIdentifier(FOUND))
    r = post_identify(admin, {"front": ("f.jpg", jpeg(), "image/jpeg")}, auth=None)
    assert r.status_code == 401


def test_identify_fills_the_form_and_holds_the_photos(admin, stub_identifier):
    stub = stub_identifier(StubIdentifier(FOUND))
    files = {"front": ("f.jpg", jpeg(), "image/jpeg"), "back": ("b.jpg", jpeg(), "image/jpeg")}
    r = post_identify(admin, files)

    assert r.status_code == 200
    assert [k for k, _ in stub.seen] == ["front", "back"]
    assert 'value="Alien"' in r.text and 'value="Zavvi"' in r.text
    assert "retailer: sticker partly peeled" in r.text
    assert "Holding front, back from identify" in r.text
    assert 'value="sealed" selected' in r.text
    assert "Identify from photos" not in r.text  # already identified
    assert "required" not in r.text.split('name="front"')[1].split(">")[0]
    assert rows("SELECT * FROM items") == []  # nothing saved until the review is
    assert (draft_root() / draft_token(r.text)).is_dir()


def test_save_after_identify_uses_the_held_photos(admin, stub_identifier):
    stub_identifier(StubIdentifier(FOUND))
    front, back = jpeg(10, 10), jpeg(20, 20)
    r = post_identify(admin, {"front": ("f.jpg", front, "image/jpeg"),
                              "back": ("b.jpg", back, "image/jpeg")})
    token = draft_token(r.text)

    r = admin.post(
        "/items", data={**FOUND.values, "draft": token}, auth=AUTH, follow_redirects=False
    )
    assert r.status_code == 303
    (item,) = rows("SELECT * FROM items")
    assert (item["title"], item["retailer"], item["upc"]) == ("Alien", "Zavvi", "036000291452")
    stored = rows("SELECT kind, path FROM photos ORDER BY id")
    assert [p["kind"] for p in stored] == ["front", "back"]
    root = Path(settings_module.settings.photo_dir)
    assert (root / stored[0]["path"]).read_bytes() == front
    assert (root / stored[1]["path"]).read_bytes() == back
    assert not (draft_root() / token).exists()


def test_the_box_tick_reaches_the_identifier(admin, stub_identifier):
    stub = stub_identifier(StubIdentifier(FOUND))
    assert 'name="boxed" type="checkbox"' in admin.get("/add", auth=AUTH).text
    admin.post("/add/identify", data={"boxed": "1"},
               files={"front": ("f.jpg", jpeg(), "image/jpeg")}, auth=AUTH)
    assert stub.boxed is True
    post_identify(admin, {"front": ("f.jpg", jpeg(), "image/jpeg")})
    assert stub.boxed is False


def test_an_other_photo_is_identified_and_kept(admin, stub_identifier):
    # A box set's number is on its bottom, not the front, spine or back.
    stub = stub_identifier(StubIdentifier(FOUND))
    number = jpeg(30, 30)
    r = post_identify(admin, {"other": ("n.jpg", number, "image/jpeg"),
                              "front": ("f.jpg", jpeg(), "image/jpeg")})
    assert [k for k, _ in stub.seen] == ["front", "other"]
    assert 'name="other" type="file"' in r.text

    r = admin.post("/items", data={**FOUND.values, "draft": draft_token(r.text)},
                   auth=AUTH, follow_redirects=False)
    assert r.status_code == 303
    stored = rows("SELECT kind, path FROM photos ORDER BY id")
    assert [p["kind"] for p in stored] == ["front", "other"]
    root = Path(settings_module.settings.photo_dir)
    assert (root / stored[1]["path"]).read_bytes() == number


def test_a_new_file_on_review_replaces_the_held_one(admin, stub_identifier):
    stub_identifier(StubIdentifier(FOUND))
    r = post_identify(admin, {"front": ("f.jpg", jpeg(10, 10), "image/jpeg")})
    retake = jpeg(30, 30)
    r = admin.post(
        "/items",
        data={"title": "Alien", "draft": draft_token(r.text)},
        files={"front": ("f2.jpg", retake, "image/jpeg")},
        auth=AUTH, follow_redirects=False,
    )
    assert r.status_code == 303
    (p,) = rows("SELECT path FROM photos")
    assert (Path(settings_module.settings.photo_dir) / p["path"]).read_bytes() == retake


def test_review_error_keeps_the_draft(admin, stub_identifier):
    stub_identifier(StubIdentifier(FOUND))
    token = draft_token(post_identify(admin, {"front": ("f.jpg", jpeg(), "image/jpeg")}).text)
    r = admin.post("/items", data={"title": " ", "draft": token}, auth=AUTH)
    assert r.status_code == 422
    assert "title is required" in r.text
    assert "a front photo is required" not in r.text
    assert draft_token(r.text) == token
    assert "Photos are not kept" not in r.text


def test_unknown_draft_is_an_error_not_a_crash(admin):
    for token in ("0" * 32, "../../etc"):
        r = admin.post("/items", data={"title": "Alien", "draft": token}, auth=AUTH)
        assert r.status_code == 422
        assert "expired" in r.text
    assert rows("SELECT * FROM items") == []


def test_identify_failure_shows_the_error_and_keeps_the_photos(admin, stub_identifier):
    stub_identifier(StubIdentifier(error="Claude API error: HTTP 529 overloaded"))
    r = post_identify(admin, {"front": ("f.jpg", jpeg(), "image/jpeg")})
    assert r.status_code == 502
    assert "identify failed: Claude API error: HTTP 529 overloaded" in r.text
    assert "Holding front from identify" in r.text


def test_identify_needs_a_front_photo(admin, stub_identifier):
    stub = stub_identifier(StubIdentifier(FOUND))
    r = post_identify(admin, {"back": ("b.jpg", jpeg(), "image/jpeg")})
    assert r.status_code == 422
    assert "a front photo is required to identify" in r.text
    assert stub.seen is None
    assert not draft_root().exists()


def test_empty_optional_file_inputs_are_ignored(admin):
    """A browser sends an unfilled file input as a part with an empty filename."""
    r = admin.post(
        "/items",
        data={"title": "Alien"},
        files={"front": ("f.jpg", jpeg(), "image/jpeg"), "spine": ("", b"", "application/octet-stream")},
        auth=AUTH, follow_redirects=False,
    )
    assert r.status_code == 303
    assert [p["kind"] for p in rows("SELECT kind FROM photos")] == ["front"]


def test_stale_drafts_are_pruned(tmp_path):
    token = photos.save_draft(str(tmp_path), [("front", ".jpg", b"x")])
    assert photos.load_draft(str(tmp_path), token) == [("front", ".jpg", b"x")]
    photos.prune_drafts(str(tmp_path), max_age=-1)
    assert photos.load_draft(str(tmp_path), token) is None


# --- re-identifying a saved item ----------------------------------------------


def save_thin(client, **data):
    """A saved item the first pass identified thinly: no UPC, no region."""
    fields = {"title": "Alien", "edition": "Steelbook", "notes": "shelf 2", **data}
    files = {"front": ("f.jpg", jpeg(10, 10), "image/jpeg"),
             "back": ("b.jpg", jpeg(20, 20), "image/jpeg")}
    r = client.post("/items", data=fields, files=files, auth=AUTH, follow_redirects=False)
    assert r.status_code == 303
    return int(r.headers["location"].rsplit("/", 1)[1])


def test_review_lists_items_missing_a_format_or_region(admin):
    thin = save_thin(admin)
    admin.post("/items", data={**FOUND.values}, files={"front": ("f.jpg", jpeg(), "image/jpeg")},
               auth=AUTH)
    no_upc = save_thin(admin, title="Heat", format="4K UHD", region="US")

    r = admin.get("/review")
    assert r.status_code == 200
    assert f'href="/items/{thin}/edit"' in r.text
    assert "no format, region" in r.text
    assert f'href="/items/{no_upc}/edit"' not in r.text  # a missing UPC alone is not flagged
    assert r.text.count("/edit\"") == 1  # the complete item is not listed
    assert "1 needs a" in admin.get("/").text


def test_reidentify_needs_credentials(admin, stub_identifier):
    item = save_thin(admin)
    stub_identifier(StubIdentifier(FOUND))
    assert admin.post(f"/items/{item}/identify").status_code == 401


def test_reidentify_fills_the_edit_form_from_the_stored_photos(admin, stub_identifier):
    item = save_thin(admin, edition="Steelbook (Box set)")
    stub = stub_identifier(StubIdentifier(Identification(
        {**FOUND.values, "edition": None, "condition": None}, ["region: guessed from ratings"]
    )))
    r = admin.post(f"/items/{item}/identify", auth=AUTH)

    assert r.status_code == 200
    assert [k for k, _ in stub.seen] == ["front", "back"]
    assert stub.seen[0][1] == jpeg(10, 10)
    assert stub.boxed  # the saved edition names a box
    assert 'value="036000291452"' in r.text and 'value="Zavvi"' in r.text
    # a field the model left blank keeps what was saved; notes are the owner's
    assert 'value="Steelbook (Box set)"' in r.text and "shelf 2" in r.text
    assert "upc: — → 036000291452" in r.text
    assert "region: guessed from ratings" in r.text
    (saved,) = rows("SELECT * FROM items")
    assert saved["upc"] is None  # nothing saved until the review is


def test_reidentify_failure_keeps_the_saved_values(admin, stub_identifier):
    item = save_thin(admin)
    stub_identifier(StubIdentifier(error="Claude declined"))
    r = admin.post(f"/items/{item}/identify", auth=AUTH)
    assert r.status_code == 502
    assert "identify failed: Claude declined" in r.text
    assert 'value="Alien"' in r.text


def test_reidentify_unknown_item_is_404(admin, stub_identifier):
    stub_identifier(StubIdentifier(FOUND))
    assert admin.post("/items/999/identify", auth=AUTH).status_code == 404
