"""Claude judging which listings are the item's edition: the clients, the worker, pricing."""

import json
from types import SimpleNamespace

import httpx
import pytest

from app import judge, main
from app import settings as settings_module
from app.judge import (
    Case,
    ClaudeJudge,
    FallbackJudge,
    JudgeError,
    ListingJudge,
    WorkerJudge,
    verdicts_from,
)
from app.pricing import Listing, SoldComps, narrow_edition

from tests.test_pricing import comp, conn, edition_row  # noqa: F401
from tests.test_worker import fake_run, jpeg, worker_client  # noqa: F401

CASE = Case(item={"title": "La La Land", "retailer": "Manta Lab"},
            listings=[{"n": 1, "title": "La La Land 4K Steelbook Manta Lab", "price": 630.0,
                       "condition": "Pre-Owned"},
                      {"n": 2, "title": "La La Land 4K Steelbook Best Buy", "price": 40.0,
                       "condition": ""}],
            front=jpeg(), pictures={2: jpeg()})


def test_the_case_text_numbers_every_listing():
    text = CASE.text()
    assert "retailer: Manta Lab" in text
    assert "1. La La Land 4K Steelbook Manta Lab — $630.00 — Pre-Owned" in text
    assert "2. La La Land 4K Steelbook Best Buy — $40.00 — condition not given [picture]" in text


def test_verdicts_default_to_other():
    raw = {"verdicts": [{"n": 1, "verdict": "same"}, {"n": 3, "verdict": "bogus"}]}
    assert verdicts_from(raw, 3) == {1: "same", 2: "other", 3: "other"}


class FakeMessages:
    def __init__(self, text, stop="end_turn"):
        self.text, self.stop, self.calls = text, stop, []

    def create(self, **kw):
        self.calls.append(kw)
        return SimpleNamespace(stop_reason=self.stop, _request_id="req_1",
                               content=[SimpleNamespace(type="text", text=self.text)])


def test_the_api_judge_sends_pictures_and_asks_for_the_schema():
    messages = FakeMessages(json.dumps({"verdicts": [{"n": 1, "verdict": "same"},
                                                     {"n": 2, "verdict": "other"}]}))
    client = SimpleNamespace(messages=messages)
    assert ClaudeJudge("k", "claude-haiku-4-5", client=client).judge(CASE) == {
        1: "same", 2: "other"}
    (call,) = messages.calls
    assert call["model"] == "claude-haiku-4-5"
    assert call["output_config"]["format"]["schema"] is judge.SCHEMA
    kinds = [b["type"] for b in call["messages"][0]["content"]]
    assert kinds.count("image") == 2 and kinds[-1] == "text"


def test_the_api_judge_raises_on_a_refusal_or_bad_json():
    for messages in (FakeMessages("{}", stop="refusal"), FakeMessages("not json")):
        with pytest.raises(JudgeError):
            ClaudeJudge("k", "m", client=SimpleNamespace(messages=messages)).judge(CASE)
    with pytest.raises(JudgeError, match="ANTHROPIC_API_KEY"):
        ClaudeJudge("", "m").judge(CASE)


def test_the_worker_judge_posts_the_case_and_pictures():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={"verdicts": [{"n": 1, "verdict": "same"}]})

    w = WorkerJudge("http://worker:8012/", "s3cret",
                    http=httpx.Client(transport=httpx.MockTransport(handler)))
    assert w.judge(CASE) == {1: "same", 2: "other"}
    (req,) = seen
    assert str(req.url) == "http://worker:8012/judge"
    assert req.headers["authorization"] == "Bearer s3cret"
    assert b'name="front"' in req.content and b'name="listing-2"' in req.content


def test_the_fallback_judge_uses_the_api_when_the_worker_fails():
    class Down:
        name = "worker"

        def judge(self, case):
            raise JudgeError("down")

    class Up:
        name = "api"

        def judge(self, case):
            return {1: "same", 2: "not_one"}

    assert FallbackJudge(Down(), Up()).judge(CASE) == {1: "same", 2: "not_one"}


def test_the_worker_judges_with_claude_code_and_only_safe_file_names(worker_client, monkeypatch):  # noqa: F811
    out = {"is_error": False, "structured_output": {"verdicts": [{"n": 2, "verdict": "not_one"}]}}
    calls = fake_run(monkeypatch, json.dumps(out))
    r = worker_client.post(
        "/judge", data={"case": CASE.text(), "n": "2"},
        files=[("front", ("front.jpg", b"F", "image/jpeg")),
               ("listing-2", ("l.jpg", b"L", "image/jpeg")),
               ("../evil", ("e.jpg", b"E", "image/jpeg"))],
        headers={"Authorization": "Bearer s3cret"})
    assert r.status_code == 200
    assert r.json() == {"verdicts": [{"n": 1, "verdict": "other"},
                                     {"n": 2, "verdict": "not_one"}]}
    (call,) = calls
    assert call.photos == ["front.jpg", "listing-2.jpg"]
    cmd = call.cmd
    assert cmd[cmd.index("--tools") + 1] == "Read"
    assert cmd[cmd.index("--system-prompt") + 1] == judge.SYSTEM
    assert "ANTHROPIC_API_KEY" not in call.env
    assert worker_client.post("/judge", data={"case": "x", "n": "1"},
                              headers={"Authorization": "Bearer no"}).status_code == 401


# --- in pricing ------------------------------------------------------------------------


def sales():
    return [Listing("La La Land 4K Steelbook Full Slip", 630.0, key="a"),
            Listing("La La Land 4K Steelbook", 700.0, key="b"),
            Listing("La La Land 4K Steelbook Best Buy", 40.0, key="c"),
            Listing("La La Land 4K Steelbook EMPTY", 5.0, key="d")]


def test_the_judges_same_listings_are_the_price(conn):  # noqa: F811
    item = edition_row(conn, retailer="Manta Lab")
    verdicts = {"a": "same", "b": "same", "c": "other", "d": "not_one"}
    chosen, matched = narrow_edition(sales(), item, 1, lambda it, ls: verdicts)
    assert ([s.key for s in chosen], matched) == (["a", "b"], "judged")


def test_too_few_same_falls_back_to_words_without_the_not_one(conn):  # noqa: F811
    item = edition_row(conn, retailer="Manta Lab")
    verdicts = {"a": "other", "b": "other", "c": "other", "d": "not_one"}
    chosen, matched = narrow_edition(sales(), item, 3, lambda it, ls: verdicts)
    assert ([s.key for s in chosen], matched) == (["a", "b", "c"], "all")


def test_a_failing_judge_is_skipped(conn):  # noqa: F811
    def broken(it, ls):
        raise JudgeError("down")

    chosen, matched = narrow_edition(sales(), edition_row(conn), 1, broken)
    assert (len(chosen), matched) == (4, "all")


def test_a_source_asks_its_judge_with_pictures(conn):  # noqa: F811
    item = edition_row(conn, retailer="Manta Lab")
    asked = []

    class Judge:
        name = "stub"

        def judge(self, case):
            asked.append(case)
            return {1: "same", 2: "other"}

    def handler(request):
        return httpx.Response(200, json={"items": [
            comp("Alien 4K Steelbook", "600.00", item_id="1", thumbnailUrl="https://i/1.jpg"),
            comp("Alien 4K Steelbook", "40.00", item_id="2", thumbnailUrl="https://i/2.jpg"),
        ]})

    source = SoldComps("k", http=httpx.Client(transport=httpx.MockTransport(handler)))
    source.judge = ListingJudge(Judge(), front=lambda item_id: jpeg(),
                                fetch=lambda url: jpeg())
    q = source.quote(item)
    assert (q.matched, q.median) == ("judged", 600.0)
    (case,) = asked
    assert sorted(case.pictures) == [1, 2] and case.front and case.item["title"] == "Alien"


def test_the_judge_is_off_by_default_and_worker_first_when_on(monkeypatch):
    s = settings_module.settings
    assert main.get_judge() is None
    monkeypatch.setattr(s, "judge_listings", True)
    monkeypatch.setattr(s, "anthropic_api_key", "k")
    assert isinstance(main.get_judge().judge_with, ClaudeJudge)
    monkeypatch.setattr(s, "worker_url", "http://worker:8012")
    monkeypatch.setattr(s, "worker_secret", "s")
    chosen = main.get_judge().judge_with
    assert isinstance(chosen, FallbackJudge) and isinstance(chosen.first, WorkerJudge)
