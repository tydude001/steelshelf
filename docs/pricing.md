# Pricing

What a case is worth, where the number comes from, and why it comes from
there. The first half is how the app prices; the second is the decision
record behind the sources.

## How an item is priced

A background job asks a pricing source for comps and appends a `valuations`
row — never overwrites — so the shelf's total is a line over time, not a
snapshot.

### What an item is worth

What an item is worth is one rule, `pricing.current_worth`, read by the shelf
total, the item page, `/stats`, the value chart and the monthly re-price
alike: its newest sold price while that is under 90 days old (eBay's sold
window) — a later ask does not outrank it — and otherwise its newest
valuation. A sold price counted across the whole film (none of its sales the
item's edition) outranks nothing and prices the item only when nothing else
does; one counted from fewer than three sales gives way to a newer ask
matched to the edition. A fetch of asks that found nothing leaves the item
unpriced; a sold lookup that found nothing hands it back to its asks. The
shelf's value tile splits the total into what sold prices and asks make of
it.

### Listings, and setting one aside

Every fetched row keeps the listings it was counted from (`listings` table),
shown on the item page with a link each. **Not this one** sets a listing
aside by its eBay item id: the price is re-counted from the rest as a new
row, and that id stays out of every later fetch, stored flagged so the page
still shows what was set aside.

### The item page's price

The item page leads with the price as **Worth about**, what was paid and the
gain beside it, the range — the middle half (quartiles) from five listings,
so one stray $965 ask moves neither end, else low to high — and a
three-block confidence cue (`pricing.confidence`: thin at four listings or
fewer, solid at twelve or more close together, fair otherwise, when the range
is wider than three quarters of the median, or when no listing named the
edition and the price is the film's every steelbook). Each valuation records
how its listings were matched (`valuations.matched`: `upc`, `judged`,
`keywords`, `retailer+region`, `retailer`, `region`, `all`, `typed`);
`/review` lists the items priced film-wide. Its history is a line once there
are two prices.

### Monthly re-price

`reprice.py`: on the 25th at 03:00 local a background thread re-prices every
item not priced since, oldest price first, through the source **Refresh
price** uses; with SerpApi it reads the free Account API first and stops with
20 of the month's searches left for refreshing by hand. A run writes only
prices it found (`valuations.via = 'monthly'`): an item with no listings
keeps its last price, a failed request is tried once more then skipped, and
one that stopped short resumes daily once searches allow. A price typed in by
hand is never re-priced over. Each run and each item's outcome is logged
(`reprice_runs`, `reprice_items`) for the status card on `/stats`, the item
page's note, and a hollow dot on the shelf's value chart for a run that
stopped short.

**Sold first:** with `SOLD_LOOKUP_ENABLED` and a `SOLDCOMPS_KEY`, the run
looks the most valuable planned items up sold on SoldComps before asking for
asks, spending at most `REPRICE_SOLD_BUDGET` (80) SoldComps requests a cycle
— SoldComps has no usage API, so the runs count their own
(`reprice_runs.sold_searches`) and the rest of the free 100 stay for the sold
button. An item with no sale, or whose lookup fails, is priced from asks as
before; a quota answer ends sold lookups for the run, not the run. The free
100 cover a 72-case shelf once a month. SerpApi's sold search is not used
monthly, since it shares the asks' quota. `REPRICE_*` in `.env` tunes it.

## Pricing source — the decision

Decided 2026-09-24.

eBay shut down the Finding API's completed-items call in February 2025. Sold
prices now live only in the Marketplace Insights API, a limited-release API
that needs eBay business approval and, per the developer forums, is not
granted to hobby developers. The Browse API returns active listings only.

**v1 uses active listings from the Browse API** (`source = 'ebay_active'`):
free and within eBay's terms. It reports the low / median / high of current
Buy It Now asks — asking prices, not sold prices. The median ask is not a
floor: sellers list above what clears, most of all for hyped titles, so asks
usually overstate. `/stats` sets the shelf's sold prices against its asks,
item by item, and gives the ratio once 20 items have both; asks are never
scaled by a guess before that. The pricing layer is one module behind an
interface so a sold-data source can replace it later. The alternatives
considered, in the order they would be tried:

| Option | Why not (yet) |
|--------|---------------|
| Paid sold-comps API (several vendors resell scraped sold data) | Built 2026-09-25 as `serpapi_sold` and 2026-09-29 as `soldcomps_sold`, disabled by default — below |
| Apply for Marketplace Insights | Cheap to try; expect no answer |
| Scrape eBay's sold-and-completed search page | Against eBay's terms and brittle; if ever added it ships disabled |

### Sold prices typed in by hand

`source = 'manual_sold'`: look the item up on eBay's site with the Sold Items
filter and enter what it cleared at on the item page. A person browsing is
within eBay's terms, and it is the one sold source with no third party in it.
Added 2026-09-25 while the developer account was rejected; it stays alongside
the API rows, not instead of them.

### Sold lookups through SerpApi

`source = 'serpapi_sold'`, added the same day. Every sold-comps vendor
surveyed scrapes eBay's sold search — none licenses it — so this is the
scraper row above at one remove and **ships disabled**
(`SOLD_LOOKUP_ENABLED=false`). SerpApi was picked for the largest free tier
(250 searches/month, one per lookup) and a sold filter in its own docs;
SoldComps ($9/month, 100 free) and OpenWeb Ninja (pay-as-you-go) were the
runners-up. The search is keyword-only (title + "steelbook", + "4K"),
filtered by `keyword_match`, US dollars only. Retailer and region stay out of
the query, since many listings omit them and a narrower search risks zero
sales; instead `prefer_edition` keeps only the sales naming the item's
retailer and region, then retailer, then region, so one film's other
steelbooks don't set the median. An ask narrows when at least three listings
name the edition; a sold lookup narrows on one, since a boutique edition may
sell only once or twice in eBay's 90 days, and on 2026-09-29 La La Land's
Manta Lab was priced at $70 from twenty Best Buy sales while its own two went
for $630 and $700. A sold lookup then narrows to copies like the item's, used
for an opened one and new for a sealed one, when any such sold; when none did
it counts them all, and the item page says the price includes the other kind
(asks do this differently, below). US never narrows: on ebay.com it is the
default and goes unsaid. With no eBay keyset, the same switch also sends
"Fetch eBay asks" through SerpApi's current listings (`serpapi_active`, Buy
It Now and best-offer only; auctions dropped), filtered the same way. An
opened item is priced from used listings when three or more are in those
results (`serpapi_active_used`), a sealed one from new listings likewise
(`serpapi_active_new`); otherwise the page says the price mixes the two. A
used-only search is not run: for one popular title it found three.

### The UPC goes first

Every title-searched source (SerpApi's sold and asks, SoldComps) searches an
item that has a UPC by the UPC, and prices what that finds as the exact
product (`matched = 'upc'`, no narrowing to the edition; a listing must still
name the film, since eBay pads a thin search with others). Only when it finds
nothing is the title searched, so such an item can spend two of the month's
searches; `Quote.searches` counts them for the monthly re-price.

### The edition's own words

Identify also returns the film's release **year**, the **edition keywords** a
seller would title a listing with ("Manta Lab, E097", "Mondo #041",
"fullslip"), and, for an edition a plain title search buries, its own **eBay
search**; all three are on the add and edit forms. Listings naming a keyword
phrase (its words together and in order) are the first narrowing tier, ahead
of retailer and region (`matched = 'keywords'`); with no keywords, the
specific parts of the edition stand in for them — "Steelbook (Mondo #041,
#710/1000)" gives "Mondo #041", never a copy's own number. The item's search
runs before the title search, which runs only if it finds nothing. The year
rejects a remake that shares the title: a listing with a year right after the
title must be within one of the film's ("The Thing (2011)" is not the 1982
case). TMDB fills the year where it is blank, picks the remake by it when the
item has one, and at startup fills it for items matched before it was kept.

### Claude judges the listings

`app/judge.py`, `JUDGE_LISTINGS`, off by default. Words narrow a title search
only as far as the listing titles say, and half a shelf names no retailer.
With the judge on, every title search's listings go to Claude with the item's
fields, its front photo, and up to 12 listing pictures, and it answers per
listing: the **same** edition, **another** steelbook of the film, or **not
one** copy (a lot, an empty case, another film). A special edition's
(fullslip, lenticular, numbered, a boutique label's or art series, or any
item with edition keywords) listing is the same only when its title or
picture shows what makes it special. The same ones are the price when there
are enough (one for a sold lookup, three for asks; `matched = 'judged'`);
otherwise the words decide as before, the not-one listings dropped. It runs
on the worker first (`POST /judge`, Claude Code on your subscription,
`WORKER_JUDGE_MODEL`, default Haiku 4.5) and falls back to the API
(`JUDGE_MODEL`, the same); a judge that fails is logged and skipped, never a
failed price. A UPC search is the exact product and is not judged. Each fetch
is one short Claude call more, which is why it is a switch.

### Sold lookups through SoldComps

`source = 'soldcomps_sold'`, added 2026-09-29 when SerpApi's sold search
returned 503 for every query while its unfiltered searches worked. It is the
same kind of scrape, so it sits behind the same switch, and runs the same
title search, filters and edition narrowing. With `SOLDCOMPS_KEY` set it
takes the sold button over from SerpApi; asks and the monthly re-price stay
where they were. It prices on `soldPrice`, the item alone, like every other
source; `totalPrice` adds shipping.
