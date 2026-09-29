"""Identify through Claude Code on the worker: the app's client, the fallback, and the worker."""

import io
import json
import subprocess
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from app import main
from app import settings as settings_module
from app.identify import (
    ClaudeVision,
    ClaudeCodeWorker,
    Fallback,
    Identification,
    IdentifyError,
)
from tests.conftest import AUTH
from tools import worker


def jpeg(w=100, h=60) -> bytes:
    out = io.BytesIO()
    Image.new("RGB", (w, h), "red").save(out, "JPEG")
    return out.getvalue()


GOOD = {
    "title": "Alien", "format": "4K UHD", "edition": "Steelbook", "retailer": "Zavvi",
    "region": "UK", "upc": "036000291452", "condition": "sealed", "doubts": [],
}


# --- the app's client ---------------------------------------------------------


def worker_client_for(handler) -> ClaudeCodeWorker:
    http = httpx.Client(transport=httpx.MockTransport(handler))
    return ClaudeCodeWorker("http://worker:8012/", "s3cret", http=http)


def test_worker_client_posts_the_photos_with_the_secret_and_parses_the_answer():
    seen = []

    def handler(request):
        seen.append(request)
        return httpx.Response(200, json={**GOOD, "upc": "036000291453"})

    found = worker_client_for(handler).identify([("front", jpeg(4000, 3000)), ("back", jpeg())], boxed=True)

    (req,) = seen
    assert str(req.url) == "http://worker:8012/identify"
    assert req.headers["authorization"] == "Bearer s3cret"
    body = req.content
    assert b'name="front"' in body and b'name="back"' in body and b'name="boxed"' in body
    assert found.values["title"] == "Alien"
    assert found.values["upc"] is None  # same checks as the API's answer
    assert found.values["edition"] == "Steelbook (Box set)"
    assert found.via == "Claude Code on the worker"


@pytest.mark.parametrize(
    "handler, match",
    [
        (lambda r: httpx.Response(502, text="Claude Code: not logged in"), "HTTP 502"),
        (lambda r: httpx.Response(200, text="<html>"), "not valid JSON"),
    ],
)
def test_worker_client_failures_are_identify_errors(handler, match):
    with pytest.raises(IdentifyError, match=match):
        worker_client_for(handler).identify([("front", jpeg())])


def test_worker_unreachable_is_an_identify_error():
    def handler(request):
        raise httpx.ConnectError("connection refused")

    with pytest.raises(IdentifyError, match="could not reach the worker"):
        worker_client_for(handler).identify([("front", jpeg())])


# --- fallback -----------------------------------------------------------------


class Stub:
    def __init__(self, name, found=None, error=None):
        self.name, self.found, self.error, self.calls = name, found, error, 0

    def identify(self, photos, boxed=False):
        self.calls += 1
        if self.error:
            raise IdentifyError(self.error)
        return self.found


FOUND = Identification({"title": "Alien"}, [], via="the Claude API")


def test_fallback_uses_the_first_when_it_answers():
    first = Stub("worker", Identification({"title": "Alien"}, via="Claude Code on the worker"))
    then = Stub("api", FOUND)
    assert Fallback(first, then).identify([("front", b"")]).via == "Claude Code on the worker"
    assert then.calls == 0


def test_fallback_uses_the_second_and_says_why():
    first, then = Stub("worker", error="worker: HTTP 502 not logged in"), Stub("api", FOUND)
    found = Fallback(first, then).identify([("front", b"")])
    assert found.values == {"title": "Alien"}
    assert found.via == "the Claude API (worker: HTTP 502 not logged in)"


def test_fallback_raises_when_both_fail():
    first, then = Stub("worker", error="down"), Stub("api", error="no key")
    with pytest.raises(IdentifyError, match="no key"):
        Fallback(first, then).identify([("front", b"")])


@pytest.fixture
def fresh_identifier(monkeypatch):
    monkeypatch.setattr(main, "_identifier", None)


def test_identifier_is_the_api_without_a_worker_url(fresh_identifier, monkeypatch):
    monkeypatch.setattr(settings_module.settings, "worker_url", "")
    assert isinstance(main.get_identifier(), ClaudeVision)


def test_identifier_tries_the_worker_first_when_configured(fresh_identifier, monkeypatch):
    monkeypatch.setattr(settings_module.settings, "worker_url", "http://worker:8012")
    identifier = main.get_identifier()
    assert isinstance(identifier, Fallback)
    assert isinstance(identifier.first, ClaudeCodeWorker)
    assert isinstance(identifier.then, ClaudeVision)


def test_the_form_says_which_route_answered(admin):
    stub = Stub("x", Identification({"title": "Alien"}, [], via="Claude Code on the worker"))
    main.app.dependency_overrides[main.get_identifier] = lambda: stub
    try:
        r = admin.post("/add/identify", files={"front": ("f.jpg", jpeg(), "image/jpeg")},
                       auth=AUTH)
    finally:
        main.app.dependency_overrides.pop(main.get_identifier, None)
    assert "Identified by Claude Code on the worker." in r.text


# --- the worker ---------------------------------------------------------------


@pytest.fixture
def worker_client(monkeypatch):
    monkeypatch.setenv("STEELSHELF_WORKER_SECRET", "s3cret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-reach-claude")
    return TestClient(worker.app)


def fake_run(monkeypatch, stdout="", returncode=0, stderr="", raises=None):
    calls = []

    def run(cmd, **kwargs):
        photos = sorted(p.name for p in kwargs["cwd"].iterdir())
        calls.append(SimpleNamespace(cmd=cmd, photos=photos, **kwargs))
        if raises:
            raise raises
        return SimpleNamespace(stdout=stdout, stderr=stderr, returncode=returncode)

    monkeypatch.setattr(worker.subprocess, "run", run)
    return calls


def claude_json(**over):
    return json.dumps({"is_error": False, "structured_output": GOOD, "result": "",
                       "duration_ms": 18000, "num_turns": 9, **over})


def post(client, secret="s3cret", files=None, data=None):
    files = files or {"front": ("front.jpg", b"F", "image/jpeg")}
    return client.post("/identify", files=files, data=data or {},
                       headers={"Authorization": f"Bearer {secret}"})


def test_worker_runs_claude_code_restricted_on_the_subscription(worker_client, monkeypatch):
    calls = fake_run(monkeypatch, claude_json())
    r = post(worker_client, files={"front": ("f.jpg", b"F", "image/jpeg"),
                                   "back": ("b.jpg", b"B", "image/jpeg")}, data={"boxed": "1"})

    assert r.status_code == 200 and r.json() == GOOD
    (call,) = calls
    assert call.photos == ["back.jpg", "front.jpg"]
    assert "ANTHROPIC_API_KEY" not in call.env  # else Claude Code bills the API key
    cmd = call.cmd
    assert "--restricted" in cmd
    assert cmd[cmd.index("--tools") + 1] == "Read,WebSearch"
    assert json.loads(cmd[cmd.index("--json-schema") + 1]) == worker.SCHEMA
    assert cmd[cmd.index("--system-prompt") + 1] == worker.SYSTEM
    assert "front.jpg, back.jpg" in cmd[2] and worker.BOXED_NOTE in cmd[2]


def test_worker_needs_the_secret(worker_client, monkeypatch):
    calls = fake_run(monkeypatch, claude_json())
    assert post(worker_client, secret="wrong").status_code == 401
    assert calls == []


def test_worker_fails_closed_without_a_secret(worker_client, monkeypatch):
    monkeypatch.delenv("STEELSHELF_WORKER_SECRET")
    assert post(worker_client, secret="").status_code == 503


def test_worker_needs_a_front(worker_client, monkeypatch):
    fake_run(monkeypatch, claude_json())
    r = post(worker_client, files={"back": ("b.jpg", b"B", "image/jpeg")})
    assert r.status_code == 422


@pytest.mark.parametrize(
    "run, status, text",
    [
        ({"stdout": claude_json(is_error=True, result="Not logged in")}, 502, "Not logged in"),
        ({"stdout": claude_json(structured_output=None, result="no json")}, 502, "no json"),
        ({"stdout": "", "stderr": "command not found", "returncode": 127}, 502, "exited 127"),
        ({"raises": subprocess.TimeoutExpired("claude", 240)}, 504, "240s"),
    ],
)
def test_worker_failures_are_http_errors(worker_client, monkeypatch, run, status, text):
    fake_run(monkeypatch, **run)
    r = post(worker_client)
    assert r.status_code == status and text in r.text
