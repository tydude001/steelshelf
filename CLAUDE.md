# CLAUDE.md — steelshelf repo

**steelshelf** — photograph a steelbook, log it, value it. Self-hosted
FastAPI + Jinja + HTMX, SQLite, Docker. The design, the pricing-source
decision and the identification pipeline are in [README.md](README.md); read
that first.

## Environment

- App deps live only in Docker; tests run from a local `.venv` (git-ignored).
  A Stop hook gates turn-end on that suite (`.claude/test-command`).
- A change to `Dockerfile` or `requirements.txt` rebuilds the image; `app/` is
  bind-mounted, so anything else is a restart.

## Rules that are decisions, not oversights

- **Asks are labelled asks, sold prices sold.** A median ask usually runs above
  what sells, so it is never passed off as a sale or scaled by a guess. Every SerpApi source
  scrapes eBay, so all sit behind `SOLD_LOOKUP_ENABLED` — never on by default.
  README § Pricing source.
- **`valuations` is append-only.** A refetch adds a row; the chart depends
  on history surviving.
- **No third party on a page-render path.** Identification and pricing run
  from a POST or a background job; pages render from the database.
- **Photos never enter git.** `data/` is ignored; keep it that way.
- **Nothing about a particular machine or install enters the repo.** Host
  names, addresses, paths, who runs it and where: not in the tree, not in a
  commit message. A pre-push hook refuses a push that carries one, and
  `tests/test_denylist.py` runs the same scan where the hook is installed.

## Conventions

- Python, FastAPI, server-rendered Jinja + HTMX (vendor htmx into
  `app/static/`, CDN-free page loads). SQLite. `ruff` at 100 columns.
- Commits `feat:` / `fix:` / `chore:`. Claude commits; a person pushes.
