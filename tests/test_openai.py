"""AI_PROVIDER=openai: the chat client, the identifier and judge on it, and the wiring."""

import io
import json

import httpx
import pytest
from PIL import Image

from app import chat, judge, main
from app import settings as settings_module
from app.identify import (
    SYSTEM,
    SYSTEM_NO_SEARCH,
    ClaudeCodeWorker,
    ClaudeVision,
    Fallback,
    IdentifyError,
    OpenAIVision,
)
from app.judge import Case, ClaudeJudge, JudgeError, OpenAIJudge


def jpeg(w=100, h=60) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), "red").save(out, "JPEG")
    return out.getvalue()


GOOD = {
    "title": "Alien", "format": "4K UHD", "edition": "Steelbook", "retailer": "Zavvi",
    "region": "UK", "upc": "036000291452", "condition": "sealed", "year": "1979",
    "edition_keywords": "", "search_query": "", "doubts": [],
}


def reply(content, finish="stop", searches=0, **message):
    return httpx.Response(200, json={
        "id": "gen-1",
        "choices": [{"finish_reason": finish,
                     "message": {"role": "assistant", "content": content, **message}}],
        "usage": {"server_tool_use": {"web_search_requests": searches}},
    })


def client_for(handler, key="sk-test") -> tuple[chat.ChatClient, list]:
    seen = []

    def record(request):
        seen.append(request)
        return handler(request)

    http = httpx.Client(transport=httpx.MockTransport(record))
    return chat.ChatClient("https://openrouter.ai/api/v1/", key, 60, http=http), seen


# --- the chat client ----------------------------------------------------------


def test_the_client_posts_the_schema_and_bearer_to_chat_completions():
    client, seen = client_for(lambda r: reply(json.dumps({"a": 1}), searches=2))
    answer = client.complete("m", "sys", [chat.text_part("hi")], {"type": "object"}, "thing")
    assert answer == chat.Answer({"a": 1}, 2)
    (req,) = seen
    assert str(req.url) == "https://openrouter.ai/api/v1/chat/completions"
    assert req.headers["authorization"] == "Bearer sk-test"
    body = json.loads(req.content)
    assert body["messages"][0] == {"role": "system", "content": "sys"}
    assert body["response_format"]["json_schema"] == {
        "name": "thing", "strict": True, "schema": {"type": "object"}}
    assert "tools" not in body


def test_a_local_server_gets_no_authorization_header():
    client, seen = client_for(lambda r: reply("{}"), key="")
    client.complete("m", "s", [], {}, "n")
    assert "authorization" not in seen[0].headers


@pytest.mark.parametrize("content", [
    '```json\n{"a": 1}\n```',
    'Here it is: {"a": 1}. Done.',
    [{"type": "text", "text": '{"a": 1}'}],
])
def test_the_answer_is_read_through_fences_and_parts(content):
    client, _ = client_for(lambda r: reply(content))
    assert client.complete("m", "s", [], {}, "n").raw == {"a": 1}


@pytest.mark.parametrize("response, match", [
    (httpx.Response(401, text="bad key"), "HTTP 401 bad key"),
    (httpx.Response(200, json={"error": "x"}), "sent no answer"),
    (reply("{}", finish="length"), "cut off"),
    (reply("{}", finish="content_filter"), "filtered"),
    (reply(None, refusal="no thanks"), "declined: no thanks"),
    (reply("not json at all"), "not valid JSON"),
])
def test_the_client_raises_on_every_failed_answer(response, match):
    client, _ = client_for(lambda r: response)
    with pytest.raises(chat.ChatError, match=match):
        client.complete("m", "s", [], {}, "n")


def test_the_client_needs_a_model():
    client, seen = client_for(lambda r: reply("{}"))
    with pytest.raises(chat.ChatError, match="OPENAI_MODEL"):
        client.complete("", "s", [], {}, "n")
    assert not seen


def test_an_unreachable_endpoint_is_a_chat_error():
    def down(request):
        raise httpx.ConnectError("refused")

    client, _ = client_for(down)
    with pytest.raises(chat.ChatError, match="could not reach openrouter.ai"):
        client.complete("m", "s", [], {}, "n")


# --- identify -----------------------------------------------------------------


def test_identify_sends_the_photos_and_parses_the_answer():
    client, seen = client_for(lambda r: reply(json.dumps({**GOOD, "upc": "036000291453"})))
    found = OpenAIVision(client, "google/gemini-flash").identify(
        [("front", jpeg(4000, 3000)), ("back", jpeg())], boxed=True)
    body = json.loads(seen[0].content)
    assert body["model"] == "google/gemini-flash"
    assert body["messages"][0]["content"] == SYSTEM_NO_SEARCH
    parts = body["messages"][1]["content"]
    assert [p["type"] for p in parts] == ["text", "image_url", "text", "image_url", "text"]
    assert parts[1]["image_url"]["url"].startswith("data:image/jpeg;base64,")
    assert "tools" not in body
    # The shared parse ran: a bad check digit is dropped, the box set named.
    assert found.values["upc"] is None and found.values["edition"] == "Steelbook (Box set)"
    assert found.via == "google/gemini-flash at openrouter.ai"


def test_identify_with_search_adds_openrouters_tool_and_the_search_prompt():
    client, seen = client_for(lambda r: reply(json.dumps(GOOD), searches=1))
    OpenAIVision(client, "m", search=True).identify([("front", jpeg())])
    body = json.loads(seen[0].content)
    assert body["messages"][0]["content"] == SYSTEM
    assert body["tools"] == [{"type": "openrouter:web_search", "parameters": {"max_uses": 3}}]


def test_the_prompt_without_search_says_nothing_of_searching():
    assert "search the web" in SYSTEM and "search the web" not in SYSTEM_NO_SEARCH
    assert SYSTEM_NO_SEARCH.endswith(SYSTEM[-200:])


def test_identify_turns_a_chat_error_into_an_identify_error():
    client, _ = client_for(lambda r: httpx.Response(500, text="boom"))
    with pytest.raises(IdentifyError, match="HTTP 500"):
        OpenAIVision(client, "m").identify([("front", jpeg())])


# --- judge --------------------------------------------------------------------

CASE = Case(item={"title": "Alien"},
            listings=[{"n": 1, "title": "Alien steelbook", "price": 30.0, "condition": ""},
                      {"n": 2, "title": "Alien lot", "price": 9.0, "condition": ""}],
            front=jpeg(), pictures={2: jpeg()})


def test_the_judge_sends_the_case_and_reads_the_verdicts():
    answer = {"verdicts": [{"n": 1, "verdict": "same"}, {"n": 2, "verdict": "not_one"}]}
    client, seen = client_for(lambda r: reply(json.dumps(answer)))
    assert OpenAIJudge(client, "m").judge(CASE) == {1: "same", 2: "not_one"}
    body = json.loads(seen[0].content)
    assert body["messages"][0]["content"] == judge.SYSTEM
    assert body["response_format"]["json_schema"]["schema"] is not None
    kinds = [p["type"] for p in body["messages"][1]["content"]]
    assert kinds.count("image_url") == 2 and kinds[-1] == "text"


def test_the_judge_raises_a_judge_error():
    client, _ = client_for(lambda r: reply("nope"))
    with pytest.raises(JudgeError):
        OpenAIJudge(client, "m").judge(CASE)


# --- wiring -------------------------------------------------------------------


@pytest.fixture
def fresh(monkeypatch):
    monkeypatch.setattr(main, "_identifier", None)
    return settings_module.settings


def test_claude_stays_the_default(fresh):
    assert fresh.ai_provider == "anthropic"
    assert isinstance(main.get_identifier(), ClaudeVision)


def test_the_openai_provider_identifies_through_the_endpoint(fresh, monkeypatch):
    monkeypatch.setattr(fresh, "ai_provider", "openai")
    monkeypatch.setattr(fresh, "openai_base_url", "http://ollama:11434/v1")
    monkeypatch.setattr(fresh, "openai_model", "qwen2.5vl")
    monkeypatch.setattr(fresh, "openai_web_search", True)
    found = main.get_identifier()
    assert isinstance(found, OpenAIVision)
    assert found.model == "qwen2.5vl" and found.search and found.client.host == "ollama"


def test_the_worker_is_still_tried_first(fresh, monkeypatch):
    monkeypatch.setattr(fresh, "ai_provider", "openai")
    monkeypatch.setattr(fresh, "worker_url", "http://worker:8012")
    found = main.get_identifier()
    assert isinstance(found, Fallback)
    assert isinstance(found.first, ClaudeCodeWorker) and isinstance(found.then, OpenAIVision)


def test_the_openai_judge_uses_its_own_model_else_the_identify_one(fresh, monkeypatch):
    monkeypatch.setattr(fresh, "judge_listings", True)
    monkeypatch.setattr(fresh, "anthropic_api_key", "k")
    assert isinstance(main.get_judge().judge_with, ClaudeJudge)
    monkeypatch.setattr(fresh, "ai_provider", "openai")
    assert main.get_judge() is None  # no model set
    monkeypatch.setattr(fresh, "openai_model", "big")
    assert main.get_judge().judge_with.model == "big"
    monkeypatch.setattr(fresh, "openai_judge_model", "small")
    chosen = main.get_judge().judge_with
    assert isinstance(chosen, OpenAIJudge) and chosen.model == "small"


def test_the_identify_copy_names_the_model(admin, monkeypatch):
    s = settings_module.settings
    assert "Asking Claude" in admin.get("/add", auth=("admin", "pw")).text
    monkeypatch.setattr(s, "ai_provider", "openai")
    page = admin.get("/add", auth=("admin", "pw")).text
    assert "Asking the model" in page and "Claude" not in page
