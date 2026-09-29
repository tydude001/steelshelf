"""Identification: photos of a steelbook in, suggested item fields out.

`ClaudeVision` sends the front / spine / back / other photos to Claude, which may
run a few web searches to place the edition, then returns the item fields as
structured JSON. `ClaudeCodeWorker` sends them to tools/worker.py instead, which
asks Claude Code the same question on your own Claude subscription;
`Fallback` tries it first and uses the API when the worker does not answer.
Everything they suggest lands in the
/add form for review — nothing is saved from here. A new identifier (a local
model, a barcode decoder) implements `Identifier` and nothing else changes,
the same shape as `pricing.PricingSource`.

Nothing here runs on a page render: the caller is the /add/identify POST.
"""

import base64
import io
import json
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Protocol

import anthropic
import httpx
import pillow_heif
from PIL import Image, ImageOps, UnidentifiedImageError

log = logging.getLogger("steelshelf.identify")

pillow_heif.register_heif_opener()  # iPhone gallery picks can arrive as HEIC

# Claude's high-resolution vision tops out at 2576px on the long edge; a
# phone photo is larger, and sticker text is the detail worth keeping.
MAX_EDGE = 2576

FORMATS = ("4K UHD", "4K + Blu-ray", "Blu-ray", "DVD")
CONDITIONS = ("sealed", "opened", "dented", "scratched")
FIELDS = ("title", "format", "edition", "retailer", "region", "upc", "condition", "year",
          "edition_keywords", "search_query")

# Anthropic's server-side web search: $10 per 1,000 searches plus the results as
# input tokens. Capped per identify; a search past the cap comes back as an error
# result the model reads, not an exception.
MAX_SEARCHES = 3
WEB_SEARCH = {"type": "web_search_20260209", "name": "web_search", "max_uses": MAX_SEARCHES}
# A long search turn can pause server-side; each resume re-sends it unchanged.
MAX_CONTINUATIONS = 3


class IdentifyError(Exception):
    """The photos could not be identified (no key, bad image, API failure, refusal)."""


@dataclass(frozen=True)
class Identification:
    values: dict[str, str | None]  # keyed by FIELDS; None where nothing was read
    doubts: list[str] = field(default_factory=list)  # shown on the form, never saved
    via: str = ""  # which route answered, shown on the form


class Identifier(Protocol):
    name: str

    def identify(
        self, photos: list[tuple[str, bytes]], boxed: bool = False
    ) -> Identification: ...


def prepare_image(data: bytes) -> bytes:
    """Any photo the form accepts → an upright JPEG no larger than MAX_EDGE."""
    try:
        img = Image.open(io.BytesIO(data))
        img = ImageOps.exif_transpose(img).convert("RGB")
    except (UnidentifiedImageError, OSError) as exc:
        raise IdentifyError(f"could not read the photo as an image: {exc}") from exc
    img.thumbnail((MAX_EDGE, MAX_EDGE))
    out = io.BytesIO()
    img.save(out, "JPEG", quality=88)
    return out.getvalue()


def upc_valid(code: str) -> bool:
    """GS1 check digit for UPC-A (12) or EAN-13 (13) — catches a misread digit."""
    if not code.isdigit() or len(code) not in (12, 13):
        return False
    digits = [int(c) for c in code]
    body, check = digits[:-1], digits[-1]
    # Weights run 3, 1, 3, … from the digit next to the check digit.
    total = sum(d * (3 if i % 2 == 0 else 1) for i, d in enumerate(reversed(body)))
    return (10 - total % 10) % 10 == check


SYSTEM = f"""You identify steelbook editions of films from photos for a collector's \
catalogue. You get the front, and usually the spine and back, of one steelbook. An \
"other" photo, when there is one, is a detail the owner thought mattered: a \
limited-edition number, the bottom of a box set, a sticker. The item may be a box \
set, fullslip or outer box with the steelbook inside, in which case the photos \
show the box. Studios and mainstream retailers make these too (e.g. a studio's \
"Ultimate Collector's Edition"), not only boutique labels: when the photos show an \
outer box or slip rather than a bare steel case, say so in the edition.

Not every steelbook is a mainstream retailer's exclusive. Boutique collector labels \
(Manta Lab, Filmarena, KimchiDVD, Blufans, Novamedia, Weet Collection, \
SteelBook Arena, and others) make limited, often numbered releases, usually in box \
sets or fullslips with no barcode on the box. They are recognised by a small label \
logo and a release code on the spine or box — Manta Lab's is a stylised M over an \
exclusive number such as "E097" — and by a hand-numbered or printed "123/1000".

Fill every field from what the photos show, plus what you know about steelbook \
releases. You can search the web, at most {MAX_SEARCHES} times, to place a release \
the photos leave open: a label's release code, a retailer exclusive, a numbered run, \
a catalogue code. Search for the specific edition (title, "steelbook", and the code \
or retailer), not the film. Skip searching when the photos already settle it. What a \
search confirms can fill a field; what it only suggests goes in doubts. Leave a field as an empty string rather than guess — an empty field is \
typed in by hand, a wrong one is easy to miss.

- title: the film's title as released, without format or edition words.
- format: one of {", ".join(FORMATS)}.
- edition: "Steelbook" unless the packaging names something more specific \
(e.g. "Steelbook (Limited)", "Steelbook (Numbered)"). Name a box set, fullslip \
or lenticular, the label's release code, and a number read from the photos with \
its run: e.g. "Steelbook (Box set, E097, #710/1000)". A release you recognise goes \
by its marketed name, e.g. "Ultimate Collector's Edition (Box set)". A printed \
catalogue or product code that is not a barcode goes here too, e.g. \
"Steelbook (Box set, cat. 1234567890)" — it is how the owner looks the release up.
- retailer: the retailer exclusive — Best Buy, Zavvi, Walmart, Target, HMV, \
Amazon, or another — from exclusive stickers, the retailer's own sticker, or \
artwork you recognise as a specific retailer's release. For a boutique release, \
the label (e.g. "Manta Lab").
- region: country of release as a two-letter code (US, UK, DE, FR, …), from \
ratings logos, barcode prefix, languages and region codes on the back, or the \
label's home country when that is all there is to go on.
- upc: the barcode digits, read from the numbers printed under the barcode. \
Digits only, and only a 12- or 13-digit barcode; any other printed code goes in \
edition.
- condition: one of {", ".join(CONDITIONS)} — "sealed" if factory shrink-wrap is \
visible, otherwise what the case shows.
- year: the film's original release year, four digits — not the steelbook's. It \
tells the film from a remake of the same title when the case is priced.
- edition_keywords: comma-separated words an eBay seller would put in a listing \
title to name this edition and no other steelbook of the same film: the label and \
its release code ("Manta Lab, E097"), a series number ("Mondo #041"), the packaging \
("fullslip", "double lenticular", "box set"). Not the title, the format, \
"steelbook", the retailer (it has its own field), or a copy's own number \
("#710/1000"). Empty for a plain retailer release.
- search_query: the eBay search that finds this edition's listings — the title, \
"steelbook", and the one or two words that single the edition out, e.g. \
"La La Land steelbook Manta Lab". Empty when the title and "steelbook" already find \
it: a narrower search that finds nothing costs a second one.
- doubts: one short sentence per field you filled but are not sure of, naming \
the field, and one for any printed code or marking you read but could not place. \
Empty if you are sure of everything you filled."""

# Told to the model when the owner ticks "box set or slip" on the form.
BOXED_NOTE = (
    "Identify this steelbook. The owner says it is a box set or slip edition: the "
    "photos show the outer packaging, with the steelbook inside. Name that in the edition."
)
BOXED_WORDS = re.compile(r"box|slip", re.IGNORECASE)

SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "format": {"type": "string", "enum": ["", *FORMATS]},
        "edition": {"type": "string"},
        "retailer": {"type": "string"},
        "region": {"type": "string"},
        "upc": {"type": "string"},
        "condition": {"type": "string", "enum": ["", *CONDITIONS]},
        "year": {"type": "string"},
        "edition_keywords": {"type": "string"},
        "search_query": {"type": "string"},
        "doubts": {"type": "array", "items": {"type": "string"}},
    },
    "required": [*FIELDS, "doubts"],
    "additionalProperties": False,
}


class ClaudeVision:
    """One Messages API call with the photos, web search and a JSON-schema output format.

    Server-side refusal fallbacks are on (`fallbacks: "default"`), so a
    classifier decline is retried on Anthropic's recommended model inside the
    same call; a refusal that survives that is an IdentifyError.
    """

    name = "claude_vision"
    BETAS = ["server-side-fallback-2026-07-01"]

    def __init__(self, api_key: str, model: str, client: anthropic.Anthropic | None = None):
        self.api_key = api_key
        self.model = model
        self._client = client

    @property
    def client(self) -> anthropic.Anthropic:
        if self._client is None:
            if not self.api_key:
                raise IdentifyError("Anthropic key missing: set ANTHROPIC_API_KEY")
            # Searches lengthen a call; 300s leaves room for the capped three.
            self._client = anthropic.Anthropic(api_key=self.api_key, timeout=300.0)
        return self._client

    def identify(self, photos: list[tuple[str, bytes]], boxed: bool = False) -> Identification:
        content: list[dict] = []
        for kind, data in photos:
            content.append({"type": "text", "text": f"{kind}:"})
            content.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/jpeg",
                    "data": base64.standard_b64encode(prepare_image(data)).decode(),
                },
            })
        content.append({"type": "text", "text": BOXED_NOTE if boxed else "Identify this steelbook."})

        messages: list[dict] = [{"role": "user", "content": content}]
        response = self._create(messages)
        for _ in range(MAX_CONTINUATIONS):
            if response.stop_reason != "pause_turn":
                break
            # Resume: the paused turn goes back as-is, with no new user message.
            messages = [messages[0], {"role": "assistant", "content": response.content}]
            response = self._create(messages)

        if response.stop_reason == "refusal":
            raise IdentifyError("Claude declined to identify these photos")
        if response.stop_reason == "max_tokens":
            raise IdentifyError("Claude's answer was cut off; try again")
        if response.stop_reason == "pause_turn":
            raise IdentifyError("Claude's search ran too long; try again")
        try:
            raw = json.loads(answer_text(response.content))
        except json.JSONDecodeError as exc:
            raise IdentifyError("Claude's answer was not valid JSON") from exc
        log.info(
            "identified %r (request %s, %d searches)",
            raw.get("title"), response._request_id, searches(response),
        )
        return replace(parse(raw, boxed), via="the Claude API")

    def _create(self, messages: list[dict]):
        try:
            return self.client.beta.messages.create(
                model=self.model,
                max_tokens=16000,
                betas=self.BETAS,
                fallbacks="default",
                system=SYSTEM,
                tools=[WEB_SEARCH],
                output_config={"format": {"type": "json_schema", "schema": SCHEMA}},
                messages=messages,
            )
        except anthropic.APIStatusError as exc:
            raise IdentifyError(f"Claude API error: HTTP {exc.status_code} {exc.message}") from exc
        except anthropic.APIConnectionError as exc:
            raise IdentifyError(f"could not reach the Claude API: {exc}") from exc


class ClaudeCodeWorker:
    """The photos, prepared here, go to tools/worker.py, which runs Claude Code.

    Same prompt and schema as ClaudeVision; the worker answers with the schema's
    JSON, parsed here like the API's. A short connect timeout, so a worker that is
    off fails over fast; a long read, since Claude Code's run takes tens of seconds.
    """

    name = "worker"

    def __init__(self, url: str, secret: str, http: httpx.Client | None = None):
        self.url = url.rstrip("/")
        self.secret = secret
        self.http = http or httpx.Client(timeout=httpx.Timeout(300.0, connect=5.0))

    def identify(self, photos: list[tuple[str, bytes]], boxed: bool = False) -> Identification:
        files = [(kind, (f"{kind}.jpg", prepare_image(data), "image/jpeg")) for kind, data in photos]
        try:
            resp = self.http.post(
                f"{self.url}/identify",
                files=files,
                data={"boxed": "1" if boxed else ""},
                headers={"Authorization": f"Bearer {self.secret}"},
            )
        except httpx.HTTPError as exc:
            raise IdentifyError(f"could not reach the worker: {exc!r}") from exc
        if resp.status_code != 200:
            raise IdentifyError(f"worker: HTTP {resp.status_code} {resp.text[:200]}")
        try:
            raw = resp.json()
        except ValueError as exc:
            raise IdentifyError("the worker's answer was not valid JSON") from exc
        log.info("identified %r on the worker", raw.get("title"))
        return replace(parse(raw, boxed), via="Claude Code on the worker")


class Fallback:
    """`first`, and `then` when `first` fails; the form says which one answered."""

    def __init__(self, first: Identifier, then: Identifier):
        self.first, self.then = first, then
        self.name = f"{first.name}, else {then.name}"

    def identify(self, photos: list[tuple[str, bytes]], boxed: bool = False) -> Identification:
        try:
            return self.first.identify(photos, boxed=boxed)
        except IdentifyError as exc:
            log.warning("%s failed, using %s: %s", self.first.name, self.then.name, exc)
            found = self.then.identify(photos, boxed=boxed)
            return replace(found, via=f"{found.via} ({exc})")


def answer_text(blocks) -> str:
    """The JSON answer: the text after the last non-text block, joined.

    With search on, a reply can open with narration ("I'll search for …") before
    the tool blocks, and citations can split the answer over several text blocks.
    """
    last_tool = max((i for i, b in enumerate(blocks) if b.type != "text"), default=-1)
    return "".join(b.text for b in blocks[last_tool + 1 :] if b.type == "text")


def searches(response) -> int:
    usage = getattr(response, "usage", None)
    tool_use = getattr(usage, "server_tool_use", None)
    return getattr(tool_use, "web_search_requests", None) or 0


def parse(raw: dict, boxed: bool = False) -> Identification:
    """Model JSON → Identification: blanks become None, a bad UPC is dropped with a doubt.

    `boxed` is the owner's word that this is a box set or slip edition; the model
    cannot reliably see that from photos of the outer packaging, so the edition is
    made to say it.
    """
    values = {f: (str(raw.get(f) or "").strip() or None) for f in FIELDS}
    doubts = [d for d in raw.get("doubts") or [] if d]
    upc = "".join(c for c in values["upc"] or "" if c.isdigit())
    if upc and not upc_valid(upc):
        doubts.insert(0, f"upc: read {upc}, which fails its check digit — left blank")
        upc = ""
    values["upc"] = upc or None
    year = values["year"]
    if year is not None:
        if year.isdigit() and 1880 <= int(year) <= 2100:
            values["year"] = int(year)
        else:
            doubts.append(f"year: read {year!r}, not a year — left blank")
            values["year"] = None
    if boxed and not BOXED_WORDS.search(values["edition"] or ""):
        values["edition"] = f"{values['edition'] or 'Steelbook'} (Box set)"
    return Identification(values, doubts)

