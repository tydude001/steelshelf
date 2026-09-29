"""Identify worker — asks Claude Code, on your own Claude subscription, to identify a steelbook
and to judge which eBay listings are its edition (POST /judge, `app.judge.WorkerJudge`).

It runs on a machine where `claude` is installed and logged in, which need not
be the app's host. The app (`app.identify.ClaudeCodeWorker`) posts one steelbook's photos,
already downscaled to JPEG, to POST /identify with a bearer secret. The worker
writes them to a temporary directory and runs Claude Code there headless, with
the app's own prompt and JSON schema, and answers with that JSON. Any failure
is an HTTP error, and the app falls back to the API.

Claude Code runs `--restricted` with only Read (confined to the photo
directory) and WebSearch, and ANTHROPIC_API_KEY is taken out of its environment
so it bills the subscription's login. One run at a time; a second request waits.

Config in ~/.config/steelshelf-worker.env (mode 600): STEELSHELF_WORKER_SECRET,
the same value as the app's WORKER_SECRET (`openssl rand -hex 32` makes one);
CLAUDE_BIN, the absolute path to `claude` (`command -v claude`; a systemd user
unit's PATH doesn't have it); and STEELSHELF_WORKER_HOST=0.0.0.0 when the app
runs on another machine, since the worker listens on 127.0.0.1 by default. It
answers only to the secret, and is still plain HTTP: keep it on a LAN or a
tailnet. Installed as a user unit from a clone at ~/projects/steelshelf (edit
the unit's two paths for a clone elsewhere), after creating its .venv:

    ln -s ~/projects/steelshelf/tools/steelshelf-worker.service ~/.config/systemd/user/
    systemctl --user daemon-reload && systemctl --user enable --now steelshelf-worker
    journalctl --user -u steelshelf-worker -f
"""

import json
import logging
import os
import re
import secrets
import subprocess
import tempfile
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app import judge
from app.identify import BOXED_NOTE, SCHEMA, SYSTEM

log = logging.getLogger("uvicorn.error")

KINDS = ("front", "spine", "back", "other")
MAX_BYTES = 20 * 1024 * 1024  # a prepared photo is a few hundred KB
TIMEOUT = 240  # seconds; under the app's 300s read timeout
MODEL = os.environ.get("WORKER_MODEL", "claude-sonnet-5")
JUDGE_MODEL = os.environ.get("WORKER_JUDGE_MODEL", "claude-haiku-4-5")
MAX_PICTURES = judge.MAX_PICTURES + 1  # the listings' and the owner's front
PICTURE_NAME = re.compile(r"front|listing-\d{1,3}")

app = FastAPI()
_one_at_a_time = threading.Lock()


def command(kinds: list[str], boxed: bool) -> list[str]:
    names = ", ".join(f"{k}.jpg" for k in kinds)
    ask = BOXED_NOTE if boxed else "Identify this steelbook."
    prompt = (
        f"Photos of one steelbook are in this directory: {names}. "
        f"Read each with the Read tool, then answer. {ask}"
    )
    return [
        os.environ.get("CLAUDE_BIN", "claude"), "-p", prompt,
        "--restricted", "--tools", "Read,WebSearch", "--allowedTools", "Read", "WebSearch",
        "--strict-mcp-config", "--no-session-persistence",
        "--model", MODEL,
        "--system-prompt", SYSTEM,
        "--json-schema", json.dumps(SCHEMA),
        "--output-format", "json",
    ]


def judge_command(pictures: list[str], case: str) -> list[str]:
    """Claude Code asked to judge listings: the case text, and the pictures to Read."""
    shown = (f"Pictures are in this directory: {', '.join(pictures)} (front.jpg is the "
             "owner's case; listing-N.jpg is listing N's). Read each with the Read tool. "
             if pictures else "")
    prompt = f"{shown}Judge every listing.\n\n{case}"
    return [
        os.environ.get("CLAUDE_BIN", "claude"), "-p", prompt,
        "--restricted", "--tools", "Read", "--allowedTools", "Read",
        "--strict-mcp-config", "--no-session-persistence",
        "--model", JUDGE_MODEL,
        "--system-prompt", judge.SYSTEM,
        "--json-schema", json.dumps(judge.SCHEMA),
        "--output-format", "json",
    ]


def run_claude(workdir: Path, kinds: list[str], boxed: bool, cmd: list[str] | None = None
               ) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        proc = subprocess.run(
            cmd or command(kinds, boxed), cwd=workdir, env=env,
            capture_output=True, text=True, timeout=TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        raise HTTPException(504, f"Claude Code took over {TIMEOUT}s") from exc
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise HTTPException(
            502, f"Claude Code exited {proc.returncode}: {(proc.stderr or proc.stdout)[:300]}"
        ) from exc
    if out.get("is_error") or not isinstance(out.get("structured_output"), dict):
        raise HTTPException(502, f"Claude Code: {str(out.get('result'))[:300]}")
    log.info(
        "answered %s in %.0fs, %d turns",
        out["structured_output"].get("title") or "a judgement",
        out.get("duration_ms", 0) / 1000, out.get("num_turns", 0),
    )
    return out["structured_output"]


@app.get("/healthz")
def healthz():
    return {"ok": True}


def check_secret(request: Request) -> None:
    secret = os.environ.get("STEELSHELF_WORKER_SECRET", "")
    if not secret:
        raise HTTPException(503, "STEELSHELF_WORKER_SECRET is not set")
    given = request.headers.get("authorization", "").removeprefix("Bearer ")
    if not secrets.compare_digest(given.encode(), secret.encode()):
        raise HTTPException(401, "bad secret")


@app.post("/judge")
async def judge_listings(request: Request):
    """Which listings are the owner's edition: `case` (the prompt text), `n` (how many
    listings), and the pictures as files named front and listing-N."""
    check_secret(request)
    form = await request.form()
    case, n = str(form.get("case") or ""), str(form.get("n") or "")
    if not case or not n.isdigit():
        raise HTTPException(422, "case and n are required")
    pictures = {}
    for name, upload in form.multi_items():
        # Only these names become files: a field name is never a path.
        if isinstance(upload, str) or not PICTURE_NAME.fullmatch(name):
            continue
        if len(pictures) >= MAX_PICTURES:
            break
        data = await upload.read()
        if len(data) > MAX_BYTES:
            raise HTTPException(413, f"{name} is over {MAX_BYTES} bytes")
        pictures[f"{name}.jpg"] = data

    def work() -> dict:
        with _one_at_a_time, tempfile.TemporaryDirectory(prefix="steelshelf-") as tmp:
            for filename, data in pictures.items():
                (Path(tmp) / filename).write_bytes(data)
            raw = run_claude(Path(tmp), [], False, judge_command(sorted(pictures), case))
            return {"verdicts": [{"n": i, "verdict": v}
                                 for i, v in judge.verdicts_from(raw, int(n)).items()]}

    return JSONResponse(await run_in_threadpool(work))


@app.post("/identify")
async def identify(request: Request):
    check_secret(request)
    form = await request.form()
    photos = {}
    for kind in KINDS:
        upload = form.get(kind)
        if upload is None or isinstance(upload, str):
            continue
        data = await upload.read()
        if len(data) > MAX_BYTES:
            raise HTTPException(413, f"{kind} photo is over {MAX_BYTES} bytes")
        photos[kind] = data
    if "front" not in photos:
        raise HTTPException(422, "a front photo is required")
    boxed = bool(form.get("boxed"))

    def work() -> dict:
        with _one_at_a_time, tempfile.TemporaryDirectory(prefix="steelshelf-") as tmp:
            for kind, data in photos.items():
                (Path(tmp) / f"{kind}.jpg").write_bytes(data)
            return run_claude(Path(tmp), list(photos), boxed)

    return JSONResponse(await run_in_threadpool(work))
