"""OpenAI-compatible chat completions: the other route to a model, beside Anthropic's.

OpenAI, OpenRouter, Ollama, LM Studio, Groq and Gemini's compatibility endpoint
all take the same POST {base}/chat/completions, so one client covers them; the
base URL and model pick which. `identify.OpenAIVision` and `judge.OpenAIJudge`
use it when AI_PROVIDER=openai. Plain httpx, so no SDK joins the image.

Photos go as base64 data URLs and the answer is asked for with the caller's JSON
schema. Not every model holds to a schema, so the answer is read leniently (a
code fence or a sentence around the object is dropped) and the caller's own
parsing does the checking. Web search is OpenRouter's `openrouter:web_search`
server tool: the model decides whether to search, capped like Claude's.
"""

import base64
import json
import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger("steelshelf.chat")


class ChatError(Exception):
    """No answer: the endpoint failed, the model declined, or it answered off-schema."""


def text_part(text: str) -> dict:
    return {"type": "text", "text": text}


def image_part(jpeg: bytes) -> dict:
    data = base64.standard_b64encode(jpeg).decode()
    return {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{data}"}}


def web_search(max_uses: int) -> dict:
    """OpenRouter's search tool; any other endpoint rejects it with an HTTP 400."""
    return {"type": "openrouter:web_search", "parameters": {"max_uses": max_uses}}


@dataclass(frozen=True)
class Answer:
    raw: dict  # the model's JSON
    searches: int  # web searches the endpoint reports having run


class ChatClient:
    def __init__(self, base_url: str, api_key: str, timeout: float,
                 http: httpx.Client | None = None):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key  # blank for a local server that takes none
        self.http = http or httpx.Client(timeout=httpx.Timeout(timeout, connect=10.0))

    @property
    def host(self) -> str:
        return httpx.URL(self.base_url).host or self.base_url

    def complete(self, model: str, system: str, content: list[dict], schema: dict,
                 name: str, tools: list[dict] | None = None) -> Answer:
        if not model:
            raise ChatError("no model set: set OPENAI_MODEL")
        body = {
            "model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": content}],
            "response_format": {"type": "json_schema",
                                "json_schema": {"name": name, "strict": True, "schema": schema}},
        }
        if tools:
            body["tools"] = tools
        headers = {"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}
        try:
            resp = self.http.post(f"{self.base_url}/chat/completions", json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise ChatError(f"could not reach {self.host}: {exc!r}") from exc
        if resp.status_code != 200:
            raise ChatError(f"{self.host}: HTTP {resp.status_code} {resp.text[:300]}")
        try:
            reply = resp.json()
            choice = reply["choices"][0]
            message = choice["message"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise ChatError(f"{self.host} sent no answer: {resp.text[:300]}") from exc
        if message.get("refusal"):
            raise ChatError(f"{model} declined: {message['refusal']}")
        finish = choice.get("finish_reason")
        if finish == "length":
            raise ChatError(f"{model}'s answer was cut off; try again")
        if finish == "content_filter":
            raise ChatError(f"{model}'s answer was filtered")
        raw = json_answer(message.get("content"))
        if raw is None:
            raise ChatError(f"{model}'s answer was not valid JSON")
        usage = reply.get("usage") or {}
        searches = (usage.get("server_tool_use") or {}).get("web_search_requests") or 0
        log.info("%s at %s answered (%s, %d searches)", model, self.host, reply.get("id"), searches)
        return Answer(raw, searches)


def json_answer(content) -> dict | None:
    """The JSON object in a reply: the whole text, or the span from its first { to last }."""
    if isinstance(content, list):  # some servers send the text as parts
        content = "".join(p.get("text", "") for p in content if isinstance(p, dict))
    if not isinstance(content, str):
        return None
    for text in (content, content[content.find("{"): content.rfind("}") + 1]):
        try:
            raw = json.loads(text)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict):
            return raw
    return None
