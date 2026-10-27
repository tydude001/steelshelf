# Competitors

Other apps that catalogue physical media, read from their store listings and
sites. This file is the evidence, not a roadmap. Prices, counts and versions
are snapshots, dated where they appear, and go stale.

**[claim]** marks something taken from a search snippet or an app's own
marketing that was not confirmed against its listing.

## Dead Media (Dead Media Labs) — competitor

Read 2026-10-27 from deadmedialabs.com (home, FAQ, import), the App Store
listing (id6761141990, raw page JSON, iTunes lookup API, all seven
screenshots) and the Google Play listing (`com.deadmedialabs.app`).

**What it is.** A free iPhone and Android app for 4K UHD, Blu-ray and DVD
collectors. It is hosted, not self-hosted. It has five parts:

- **Shelf:** real spines at real widths, with filters for steelbooks,
  Criterions, blind buys and unwatched discs.
- **Database:** barcode lookup, which the listing says covers over 100,000
  releases built by collectors.
- **Reviews:** a social feed, follows, and Letterboxd ratings history.
- **Market:** buy and sell between users, with phone-verified sellers, ratings
  and no fees. Payment happens off the app.
- **Collector's Report:** counts by format, label, director, decade and genre.

Smaller features: a release calendar, a Movie of the Week and shareable
snapshots.

**Prices are what you paid, not what it's worth.** The only price screen is
"Spending" (screenshot 5, *"Track the cost of your collection… or don't."*).
It shows total spent, average price and how many discs have a price, then
lists each disc's price. In the screenshot 27 of 181 discs are tracked, so a
price is entered per disc by hand. The description's one valuation phrase,
*"what it's all worth,"* sits under the Report. No listing or page names a
market source: no eBay, sold listings or price guide.

**Paid features.** The site says "free to download". Google Play marks the app
"In-app purchases, $3.99 – $39.99 per item". What those buy is not stated; a
$3.99/$39.99 pair usually means monthly and yearly, but that is a guess. The
App Store's data has `hasInAppPurchases: false` for iOS 1.0.0, released
2026-10-14. Other figures: 9 ratings averaging 4.9 on iOS, 500+ installs on
Android, developer Bymike LLC.

**Onboarding.** Bulk scan works through a stack of barcodes and then offers
"Add 5 to Collection" (screenshot 2). Import reads Letterboxd, Blu-ray.com,
CLZ, My Movies Pro and CSV.

**Against steelshelf.**

| | Dead Media | steelshelf |
|---|---|---|
| Runs as | a hosted service, iOS and Android apps | your own server |
| Scope | 4K, Blu-ray, DVD | steelbooks |
| Identify | barcode only | barcode or a photo read by a vision model, then a draft to confirm |
| Price | what you paid, typed in | what it's worth from the market, kept as history beside what you paid |
| Social | market, reviews, follows | none |

**Worth taking:** bulk capture (shoot many cases, confirm the drafts later)
and CSV import and export.

**Not taken:** the market and the social features, which need a network of
users. Covering every disc format is also out, because it would dilute the
steelbook-specific edition matching ([pricing.md](pricing.md)).

## Seen in passing — not yet read

These turned up in the same search and do the part Dead Media doesn't: market
value. Each needs a proper read before any comparison.

- **Keptcase** (iOS) [claim]: "live market values on every shelf" with paid
  vs. worth and the gain, and a dated PDF inventory for insurance. It covers
  many media types. A 7-day trial, then $4.99 a month or $39.99 a year; after
  that, values need the subscription.
- **WatchRoster** (web) [claim]: a price guide built from completed eBay
  sales, with alerts when a title drops below a target price.
- **Movie Vault** (web) [claim]: prices from active eBay listings through the
  Browse API, so asking prices, not sales.
- **Movie Collector Shelf** (iOS) [claim]: steelbooks included; Pro costs
  $19.99 a year and adds bulk scan and alerts when a price spikes, drops or
  goes out of print.
