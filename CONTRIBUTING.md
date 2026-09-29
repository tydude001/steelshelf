# Contributing to steelshelf

Issues and pull requests are welcome. steelshelf is maintained by one person
in their spare time, so a reply can take a while.

## Before you start

For anything bigger than a bug fix, open an issue first. The README says what
the app does; [docs/](docs/) explains how it identifies, prices and lays out
the shelf, and why; a change that fights the rules below will usually be
turned down however good the code is.

## The rules a pull request is checked against

- **Asks are labelled asks; sold prices are labelled sold.** Active listings
  are what things are offered at, not what they clear at — a median ask
  usually runs above what sells — and the pages say so; an ask is never
  scaled by a guess. Every source that scrapes eBay's sold search (SerpApi,
  SoldComps, and any vendor like it) sits behind `SOLD_LOOKUP_ENABLED`, which
  is never on by default. [docs/pricing.md](docs/pricing.md#pricing-source--the-decision)
  has the argument.
- **`valuations` is append-only.** A refetch adds a row and never overwrites
  one; the value-over-time chart depends on the history surviving.
- **No third party on a page-render path.** Identification, pricing and film
  lookups run from a POST or a background job; pages render from the
  database, so a slow or dead API never slows a page.
- **Photos never enter git.** `data/` is ignored, and so is `demo/`.
- **No real collection data in fixtures, issues or pull requests.** Test
  images are generated and test items invented; `scripts/demo.py` is the
  model. Real steelbook art belongs to the studios and artists, and a real
  shelf is a record of what someone owns and paid.
- **Don't edit an existing test to make it pass.** If your change and a test
  disagree, say so in the pull request — the test may be the one that's
  right.
- **Every colour is a CSS token**, redefined for the dark scheme
  (`tests/test_theme.py` checks it), and page loads stay CDN-free: vendor
  what a page needs into `app/static/`.

## Running the checks

```sh
python -m venv .venv
.venv/bin/pip install -r requirements.txt -r requirements-dev.txt
.venv/bin/python -m ruff check .
.venv/bin/python -m pytest -q
```

The suite needs no network and no keys.

## Commits

Prefix commit messages with `feat:`, `fix:`, `chore:` or `docs:`.

## How a pull request lands

GitHub is a read-only mirror of the maintainer's own git server, so a pull
request is never merged with GitHub's merge button. The maintainer fetches
your branch, merges it on their side, and the next mirror sync carries it up;
GitHub then marks the pull request merged on its own once your head commit
reaches `main`. If the merge has to be squashed or reworked, the SHAs change
and the pull request is closed by hand with a note naming the commit your
work landed in. Either way, nothing is lost and you'll be credited in the
commit.

By contributing, you agree your contribution is licensed under this
project's [MIT licence](LICENSE).
