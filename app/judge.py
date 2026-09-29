"""Judging listings: which of a title search's listings are the owner's edition.

A title search takes every steelbook of the film, and the word filters in
`pricing` narrow them only as far as the listing titles say. `ClaudeJudge` asks
Claude instead: given the item (title, year, format, edition, retailer, region,
edition keywords, condition), a photo of the owner's case, and each listing's
title, price, condition and picture, it answers per listing — `same` edition,
`other` steelbook of the film, or `not_one` copy (a lot, an empty case, discs
only, another film). `WorkerJudge` asks Claude Code on the worker the same
question on your own subscription; `FallbackJudge` tries the worker, then the API.

`ListingJudge` is what the pricing sources hold: it reads the owner's front
photo, fetches up to `MAX_PICTURES` listing pictures, asks, and returns the
verdicts by listing. The source then prices from the `same` listings when there
are enough (`matched = 'judged'`) and otherwise narrows by words as before,
with the `not_one` listings dropped either way. A judge that fails is logged and
skipped: pricing never waits on it to succeed.

Nothing here runs on a page render: the callers are a price POST and the
monthly re-price.
"""

import base64
import io
import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

import anthropic
import httpx
from PIL import Image, ImageOps, UnidentifiedImageError

log = logging.getLogger("steelshelf.judge")

VERDICTS = ("same", "other", "not_one")
MAX_PICTURES = 12  # listing pictures sent per judgement; the rest go by title
MAX_LISTINGS = 100  # a search page; more are judged by title filters alone
PICTURE_EDGE = 512  # a listing thumbnail is 500px; the owner's photo is cut to match

SYSTEM = """You sort eBay listings for a steelbook collector who wants to price one \
particular edition. You get the owner's item — its fields and, usually, a photo of \
its front — and a numbered list of listings a search for the film found, some with a \
picture. For each listing, answer one verdict:

- same: one copy of the owner's edition — the same release by the same retailer or \
label, in the same packaging (a fullslip, lenticular or box set is its own edition), \
in the same format. A listing that names nothing contrary and whose picture matches \
the owner's front counts. Condition does not matter here.
- other: one copy of a different steelbook or release of the same film: another \
retailer's or label's, another country's, another format's, a plain case where the \
owner's is a box set, or a non-steelbook release.
- not_one: not one copy of any release of the film: a lot or bundle, an empty case, \
discs or a slip only, a poster, or a different film (a sequel, a remake or reboot of \
another year, or a film whose title contains this one's — The Amazing Spider-Man is \
not Spider-Man).

Judge from the title first and the picture second; when the two leave it open, \
answer other rather than same: a wrong same moves the price, a wrong other only \
drops one listing. Answer every listing, by its number."""

SCHEMA = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "n": {"type": "integer"},
                    "verdict": {"type": "string", "enum": list(VERDICTS)},
                },
                "required": ["n", "verdict"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

ITEM_FIELDS = ("title", "year", "format", "edition", "edition_keywords", "retailer", "region",
               "condition", "notes")


class JudgeError(Exception):
    """The listings could not be judged (no key, network, refusal, a bad answer)."""


@dataclass(frozen=True)
class Case:
    """What a judge is asked about: the item, its listings, and the pictures."""

    item: dict  # ITEM_FIELDS, blanks dropped
    listings: list[dict]  # {"n", "title", "price", "condition"}, numbered from 1
    front: bytes | None  # the owner's front photo, as a JPEG
    pictures: dict[int, bytes]  # listing number → its picture, as a JPEG

    def text(self) -> str:
        """The item and the numbered listings, as the prompt puts them."""
        lines = ["The owner's item:"]
        lines += [f"  {k}: {v}" for k, v in self.item.items()]
        lines.append("")
        lines.append("Listings:")
        for ls in self.listings:
            pic = " [picture]" if ls["n"] in self.pictures else ""
            lines.append(f"  {ls['n']}. {ls['title']} — ${ls['price']:.2f}"
                         f" — {ls['condition'] or 'condition not given'}{pic}")
        return "\n".join(lines)


def verdicts_from(raw: dict, n: int) -> dict[int, str]:
    """The answer's verdicts by listing number; one missing or unknown is `other`."""
    got = {}
    for v in raw.get("verdicts") or []:
        if isinstance(v, dict) and v.get("verdict") in VERDICTS and isinstance(v.get("n"), int):
            got[v["n"]] = v["verdict"]
    return {i: got.get(i, "other") for i in range(1, n + 1)}


class Judge(Protocol):
    name: str

    def judge(self, case: Case) -> dict[int, str]: ...


def _jpeg(data: bytes, edge: int = PICTURE_EDGE) -> bytes | None:
    """Any image → an upright JPEG no larger than `edge`; None when it will not decode."""
    try:
        img = ImageOps.exif_transpose(Image.open(io.BytesIO(data))).convert("RGB")
    except (UnidentifiedImageError, OSError):
        return None
    img.thumbnail((edge, edge))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=85)
    return out.getvalue()


def _b64(data: bytes) -> dict:
    return {"type": "image", "source": {"type": "base64", "media_type": "image/jpeg",
                                        "data": base64.standard_b64encode(data).decode()}}


class ClaudeJudge:
    """One Messages API call: the item, the pictures, the listings; JSON verdicts back."""

    name = "claude_api"

    def __init__(self, api_key: str, model: str, client: anthropic.Anthropic | None = None):
        self.api_key, self.model, self._client = api_key, model, client

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            if not self.api_key:
                raise JudgeError("Anthropic key missing: set ANTHROPIC_API_KEY")
            self._client = anthropic.Anthropic(api_key=self.api_key, timeout=120.0)
        return self._client

    def judge(self, case: Case) -> dict[int, str]:
        content: list[dict] = []
        if case.front:
            content += [{"type": "text", "text": "The owner's case, front:"}, _b64(case.front)]
        for n, pic in sorted(case.pictures.items()):
            content += [{"type": "text", "text": f"Listing {n}'s picture:"}, _b64(pic)]
        content.append({"type": "text", "text": case.text()})
        try:
            response = self.client.messages.create(
                model=self.model,
                max_tokens=8000,
                system=SYSTEM,
                output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
                messages=[{"role": "user", "content": content}],
            )
        except anthropic.APIStatusError as exc:
            raise JudgeError(f"Claude API error: HTTP {exc.status_code} {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise JudgeError(f"could not reach the Claude API: {exc}") from exc
        if response.stop_reason in ("refusal", "max_tokens"):
            raise JudgeError(f"Claude stopped: {response.stop_reason}")
        text = "".join(b.text for b in response.content if b.type == "text")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise JudgeError("Claude's verdicts were not valid JSON") from exc
        log.info("judged %d listings (request %s)", len(case.listings), response._request_id)
        return verdicts_from(raw, len(case.listings))


class WorkerJudge:
    """The case goes to tools/worker.py's /judge, which asks Claude Code the same."""

    name = "worker"

    def __init__(self, url: str, secret: str, http: httpx.Client | None = None):
        self.url = url.rstrip("/")
        self.secret = secret
        self.http = http or httpx.Client(timeout=httpx.Timeout(300.0, connect=5.0))

    def judge(self, case: Case) -> dict[int, str]:
        files = []
        if case.front:
            files.append(("front", ("front.jpg", case.front, "image/jpeg")))
        for n, pic in sorted(case.pictures.items()):
            files.append((f"listing-{n}", (f"listing-{n}.jpg", pic, "image/jpeg")))
        try:
            resp = self.http.post(
                f"{self.url}/judge",
                data={"case": case.text(), "n": str(len(case.listings))},
                files=files or None,
                headers={"Authorization": f"Bearer {self.secret}"},
            )
        except httpx.HTTPError as exc:
            raise JudgeError(f"could not reach the worker: {exc!r}") from exc
        if resp.status_code != 200:
            raise JudgeError(f"worker: HTTP {resp.status_code} {resp.text[:200]}")
        try:
            raw = resp.json()
        except ValueError as exc:
            raise JudgeError("the worker's verdicts were not valid JSON") from exc
        return verdicts_from(raw, len(case.listings))


class FallbackJudge:
    """`first`, and `then` when `first` fails."""

    def __init__(self, first: Judge, then: Judge):
        self.first, self.then = first, then
        self.name = f"{first.name}, else {then.name}"

    def judge(self, case: Case) -> dict[int, str]:
        try:
            return self.first.judge(case)
        except JudgeError as exc:
            log.warning("%s failed, using %s: %s", self.first.name, self.then.name, exc)
            return self.then.judge(case)


class ListingJudge:
    """What a pricing source calls: listings in, a verdict per listing out (by key).

    `front` returns the item's front photo bytes (None without one); `fetch` gets a
    picture URL's bytes. Pictures that will not fetch or decode are left out.
    """

    def __init__(self, judge: Judge, front: Callable[[int], bytes | None],
                 fetch: Callable[[str], bytes] | None = None):
        self.judge_with, self.front = judge, front
        self.fetch = fetch or _fetch

    def __call__(self, item, listings: list) -> dict[str, str]:
        listings = list(listings)[:MAX_LISTINGS]
        if not listings:
            return {}
        pictures: dict[int, bytes] = {}
        for n, ls in enumerate(listings, 1):
            if len(pictures) >= MAX_PICTURES:
                break
            if ls.thumb:
                try:
                    pic = _jpeg(self.fetch(ls.thumb))
                except httpx.HTTPError as exc:
                    log.info("listing picture not fetched: %s", exc)
                    continue
                if pic:
                    pictures[n] = pic
        front = self.front(item["id"])
        case = Case(
            item={f: item[f] for f in ITEM_FIELDS if f in item.keys() and item[f]},
            listings=[{"n": n, "title": ls.title, "price": ls.price, "condition": ls.condition}
                      for n, ls in enumerate(listings, 1)],
            front=_jpeg(front) if front else None,
            pictures=pictures,
        )
        verdicts = self.judge_with.judge(case)
        return {ls.key: verdicts[n] for n, ls in enumerate(listings, 1)}


_http = httpx.Client(timeout=10.0, follow_redirects=True)


def _fetch(url: str) -> bytes:
    r = _http.get(url)
    r.raise_for_status()
    return r.content
