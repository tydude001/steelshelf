"""Identify worker — asks Claude Code, on your own Claude subscription, to identify a steelbook.

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
import secrets
import subprocess
import tempfile
import threading
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from starlette.concurrency import run_in_threadpool

from app.identify import BOXED_NOTE, SCHEMA, SYSTEM

log = logging.getLogger("uvicorn.error")

KINDS = ("front", "spine", "back", "other")
MAX_BYTES = 20 * 1024 * 1024  # a prepared photo is a few hundred KB
TIMEOUT = 240  # seconds; under the app's 300s read timeout
MODEL = os.environ.get("WORKER_MODEL", "claude-sonnet-5")

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


def run_claude(workdir: Path, kinds: list[str], boxed: bool) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    try:
        proc = subprocess.run(
            command(kinds, boxed), cwd=workdir, env=env,
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
        "identified %r in %.0fs, %d turns",
        out["structured_output"].get("title"),
        out.get("duration_ms", 0) / 1000, out.get("num_turns", 0),
    )
    return out["structured_output"]


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.post("/identify")
async def identify(request: Request):
    secret = os.environ.get("STEELSHELF_WORKER_SECRET", "")
    if not secret:
        raise HTTPException(503, "STEELSHELF_WORKER_SECRET is not set")
    given = request.headers.get("authorization", "").removeprefix("Bearer ")
    if not secrets.compare_digest(given.encode(), secret.encode()):
        raise HTTPException(401, "bad secret")

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
