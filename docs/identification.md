# Identification

How a case gets from three phone photos to a saved row: what is captured, how
it is named, and what is kept. The model choice behind it is at the end.

## Capture

From the phone (PWA, `<input capture>`), take front, spine and back photos of
a steelbook, and optionally an **other** detail shot — a limited-edition
number, the bottom of a box set, a sticker. A **box set or slip** tick tells
the model the photos show outer packaging, which it cannot reliably see for
itself; the saved edition then always names it.

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
(`PHOTO_DIR/_drafts/<token>/`, pruned after a day), re-renders `/add` filled
in with the model's doubts listed, and **Save** writes the item from the
draft. A UPC that fails its GS1 check digit is dropped, not saved. A saved
item's fields stay editable (**Edit** at the top of its page); its photos do
not. **Identify again from photos** on the edit page re-runs identification
on the stored photos and fills the form for review, listing what changed;
`/review` lists the items saved without a format or region, the ones worth a
rerun (a missing UPC is not flagged: most cases carry none).

**You paid** and **Bought on** are typed, never identified; the item page sets
the paid price against the worth, and the shelf sums both.

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
