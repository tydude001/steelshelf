# Identification

How a case gets from three phone photos to a saved row: what is captured, how
it is named, and what is kept — one case at a time, or a whole shelf shot in
one go and confirmed later. The model choice behind it is at the end.

## Capture

From the phone (PWA, `<input capture>`), take front, spine and back photos of
a steelbook, and optionally an **other** detail shot — a limited-edition
number, the bottom of a box set, a sticker. A **box set or slip** tick tells
the model the photos show outer packaging, which it cannot reliably see for
itself; the saved edition then always names it.

That is one case, identified while you wait (15–20 s on the worker) and
confirmed on the spot. **Bulk capture** is the same form with a second
button, **Queue and next**: it uploads the photos, answers as soon as they
are on disk, and hands back an empty form with a count ("7 queued · 3
ready"), so a full shelf on day one is shoot, tap, shoot, tap. Identifying
happens behind you, and confirming waits for a desk (§ Bulk capture below).
Dead Media's bulk scan is the reference ([COMPETITORS.md](COMPETITORS.md)),
with one difference that is the whole point of steelshelf: its scan reads
barcodes, and most steelbooks have none, so a queued case here carries
photos and is read by the model like any other.

## Identify

Two signals, used together:

- **Barcode first.** Decode the UPC on the back or on the retailer sticker
  locally (`barcode.py`, zxing-cpp; the GS1 check digit confirms it, and the
  decode is taken over the digits Claude reads, with a doubt noted when they
  differ). eBay's Browse API searches by UPC directly
  (`item_summary/search?gtin=`), which lands on the exact product. Most
  steelbook cases carry no barcode — it is on the slip or a sticker — so a
  miss is usual: on a real shelf of 72 cases the decoder read every code
  Claude had read and none Claude had missed.
- **Vision second.** Send the photos to Claude for title, format, retailer
  exclusive (Best Buy / Zavvi / Walmart / …) or boutique label (Manta Lab /
  Filmarena / … — spine logo and release code, numbered run into `edition`),
  region and condition notes — the things a barcode misses, and the whole
  answer when there is no readable code. Claude may run up to three web
  searches (Anthropic's server-side tool, $10 per 1,000 plus the results as
  input tokens) to place a release code, exclusive or numbered run the photos
  leave open. eBay's Browse API image search is a further signal to try.

Identify also returns the film's release **year**, the **edition keywords** a
seller would title a listing with, and, for an edition a plain title search
buries, its own **eBay search**. Pricing uses all three; see
[pricing.md § The edition's own words](pricing.md#the-editions-own-words).

## Log

One `items` row, a `photos` row per photo. Edit anything the model got wrong
before saving: **Identify from photos** holds the photos as a draft
(`PHOTO_DIR/_drafts/<token>/`, pruned after a day unless queued), re-renders `/add` filled
in with the model's doubts listed, and **Save** writes the item from the
draft. A UPC that fails its GS1 check digit is dropped, not saved. A saved
item's fields stay editable (**Edit** at the top of its page); its photos do
not. **Identify again from photos** on the edit page re-runs identification
on the stored photos and fills the form for review, listing what changed;
`/review` lists the items saved without a format or region, the ones worth a
rerun (a missing UPC is not flagged: most cases carry none).

**You paid** and **Bought on** are typed, never identified; the item page sets
the paid price against the worth, and the shelf sums both.

## Bulk capture

The single-case path holds its draft only on disk, for a day, because the
form it feeds is open in front of you. A queued case waits days for its desk
session and has to remember what identify said, so bulk capture gives drafts a
row of their own; everything after that is the pipeline above, run later.

**Drafts.** A `drafts` table, one row per queued case, beside the photos in
`PHOTO_DIR/_drafts/<token>/` as now:

| Column | Holds |
|---|---|
| `token` | the draft directory's name, the key |
| `status` | `queued` → `identifying` → `ready`, or `failed` |
| `boxed` | the box-or-slip tick, sent with the photos |
| `upc` | the locally decoded barcode, if any |
| `found` | identify's answer as JSON — values, doubts and who answered (`via`) |
| `error` | why it failed: no model set up, a refusal, photos gone |
| `attempts` | identify calls made for it |
| `created_at`, `identified_at` | UTC stamps, as every table has them |

A draft is not an item: the shelf, `/stats`, the re-price and `/healthz`
never see it. `prune_drafts` leaves alone any directory with a row, so a
queued case is kept until it is saved or discarded; a single-case draft, with
no row, still goes after a day.

**The queue.** One background thread, the film lookup's pattern
(`main.look_up_films`): woken by each new draft and at startup, it takes the
oldest `queued` row, marks it `identifying`, and runs exactly what
`POST /add/identify` runs — `barcode.decode_any`, then
`get_identifier().identify(shots, boxed=…)`, then `barcode.apply` — and writes
`found` and `ready`. One case at a time: the worker is one `claude -p`
process, and a shelf of 200 at 15–20 s each is about an hour, done before the
desk session starts. Pricing stays out of the queue: an item is priced once
saved, by **Refresh price** or the re-price run, whose plan takes
never-priced items first ([pricing.md](pricing.md)), so **Run the rest now**
on `/stats` prices a freshly confirmed batch. Nothing here puts a third party
on a page: the queue is a background job and `/drafts` renders from the
table.

**At the desk.** `/drafts` (admin, like `/add`) lists the queue oldest first:
the front thumbnail (made on first request, kept in the draft's directory as
`thumb.jpg`), the title and edition identify found, how many doubts, who
answered, and the status. Each opens the existing `/add` review form filled
from `found` (`GET /add?draft=<token>`); one not yet identified opens empty
with its photos held, to type by hand. **Save** is the existing `POST /items`
with that draft, which also deletes the row in the same transaction and, for
a queued draft, returns to `/drafts` naming what was saved (a single-case
save still goes to the item). Every case is confirmed one by one: the model's
answer is a draft to check, here as on the phone. **Discard** drops the row
and the photos.

**When it goes wrong.**

- **No front photo:** refused at **Queue and next**, nothing kept; identify
  needs one.
- **Upload fails** (the phone loses the network): the form says so and that
  case is shot again; the browser keeps no file input across a failed post.
- **Identify fails** — no key or worker, the worker down with no API to fall
  back to, a refusal: `failed` with the reason. The draft still opens in the
  form to type by hand, and **Identify again** re-queues it. An exception that
  is not an `IdentifyError` (a bug) fails the draft too, as "unexpected error",
  so the queue moves on instead of leaving it `identifying`.
- **The app restarts mid-queue:** startup puts any `identifying` row back to
  `queued`; at most one call is repeated.
- **Photos gone** (a draft directory deleted by hand): `failed`, "photos
  gone", with no identify call made, and only **Discard** is offered —
  `/drafts` checks the directory, so it says so before the queue gets there.
- **The same case shot twice,** easy when working along a shelf: an
  identified draft whose UPC, or title and edition, matches an item or an
  earlier draft is marked *possibly a repeat* on `/drafts`, naming and linking
  the case it matches. Title and edition are import's `csvio.repeat_key`
  without the retailer, which identify can read off one shot of a case and
  miss on the next. Only the later of two drafts is marked, and the mark
  warns: the draft saves as any other.
- **Photos of two cases in one draft** (front of one, back of the next): not
  detectable; the review form shows every held photo, which is where it is
  seen.
- **Cost:** each queued case is one identify, as it would be alone. With the
  worker over its limit, `Fallback` answers from the API at cents per case,
  and `/drafts` names who answered each.

**Built in the first slice:** the `drafts` table, **Queue and next** (`POST
/drafts`, 303 to `/add?queued=1`), the queue thread (`main.identify_queue`,
one long-lived daemon woken by an event; `drain_queue` is the synchronous
loop the tests drive) with the startup reset, `/drafts` with **Save** through
the existing form and **Discard**, and `prune_drafts` sparing queued
directories; then the repeat mark. Not built: **Identify again** for a failed
draft, and the meta refresh — `/drafts` is reloaded by hand. A
save-without-review shortcut for drafts with no doubts is left out until the
confirm rate on a real shelf says it is safe.

## Vision model — the decision

Decided 2026-09-24. **Claude does the vision.**

- **Vision.** Naming the retailer exclusive and the edition is recognition
  more than reading, and a 7B local VLM (Qwen2.5-VL, say) has little of that
  knowledge. Claude does it.
- **Which Claude, 2026-09-25.** Claude Code on the worker first, on your own
  Claude subscription: `tools/worker.py` runs it headless (`claude -p
  --restricted`, Read and WebSearch only) with the app's prompt and schema, in
  about 15–20 s. When the worker is off, logged out or over its limits, the
  app falls back to the API key — cents per item — and the form says which
  one answered. Running the `claude` CLI from a script stays inside Claude
  Code; the subscription's login is never lifted into the app itself, which
  the terms forbid (README § Keys). The worker is optional and runs *your
  own* `claude` login on *your own* machine; whether that use fits your
  plan's terms is between you and Anthropic.

`identify.py` puts the model behind an `Identifier` interface, like pricing,
so a local model or a barcode decoder can replace or join it without touching
the routes.

## Other models

Added 2026-09-29, for installs without an Anthropic key. `AI_PROVIDER=openai`
sends identify and the listing judge to any endpoint that speaks OpenAI's chat
completions — OpenAI, OpenRouter, Ollama, LM Studio, Groq, Gemini's
compatibility API — through `chat.py`, plain httpx with no SDK. Claude stays
the default; the worker, when set, is still tried first.

The prompt, the schema and the parsing are the ones Claude gets: photos go as
data URLs, the schema as `response_format`, and `parse` checks the answer the
same way, UPC check digit and all. What differs:

- **Search.** `OPENAI_WEB_SEARCH=true` adds OpenRouter's
  `openrouter:web_search` server tool, capped at the same three searches and
  with the model deciding whether to search, as with Claude. Other endpoints
  reject that tool, so there it stays off and the prompt drops its search
  rules: the model answers from the photos and what it knows, and a boutique
  release code it cannot place goes in the doubts.
- **Schema holding.** Not every model enforces a schema. The answer is read
  from the first `{` to the last, so a code fence or a sentence around it
  does no harm; a field outside the schema is ignored and one missing is
  left blank for you to type.
- **What Claude-only features did.** Anthropic's server-side refusal
  fallback and a paused search turn's resume have no counterpart; a refusal,
  a filtered answer or a cut-off one is an error on the form.
- **Recognition.** The reason Claude does the vision (above) holds: a small
  local model reads the title and the barcode but names few retailer
  exclusives or boutique labels. A large hosted model through OpenRouter
  comes closer. Nothing here was tuned on anything but Claude.
