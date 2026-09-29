# Deploying steelshelf

One container, one port, one directory of state. This is the recipe for a
home server; the README's quick start is the same thing in three lines.

## Compose

```sh
git clone https://github.com/tydude001/steelshelf.git
cd steelshelf
cp .env.example .env        # or scripts/set-secrets.sh, which checks each key
docker compose up -d --build
curl http://localhost:8010/healthz
```

`data/` holds everything that is yours: `steelshelf.db` and `photos/`. It is
bind-mounted into the container, git-ignored, and the only thing to back up.
The repo ships an empty `data/` so the first `up` has a mount source; some
Docker hosts refuse to bind-mount a missing directory rather than create it.

Compose also mounts the clone's `app/` over the image's copy, read-only, so
after a `git pull` a code change is a `docker compose restart` (~15 s). Only a
change to `Dockerfile` or `requirements.txt` needs `--build`. A change to
`.env` needs `docker compose up -d --force-recreate`: a restart keeps the
environment the container was created with.

Set `TZ` in `.env` (e.g. `Europe/London`) so pages show dates in your zone;
the database stamps UTC, and so does the image unless told otherwise.

## The port is the boundary

The app listens on 8010 with no TLS, and every read is open: anyone who can
reach the port can see the shelf and its photos. Writes and anything that
spends an API key need the admin login (HTTP Basic, sent in plain text
without TLS). So:

- **On a LAN or a private overlay network** (Tailscale, WireGuard): run it
  as is. This is what it is built for.
- **Anywhere else**: put a reverse proxy with TLS in front (Caddy, Traefik,
  nginx), and consider the proxy's own auth for reads. Don't publish 8010
  itself.

Change `ADMIN_PASSWORD` from `changeme`; the app logs a warning at startup
until you do.

## Health

`GET /healthz` runs a count over the items table and answers 503 if the
database or the bind mount is gone. The image's `HEALTHCHECK` uses it, and it
is the one URL to point an uptime probe at; `/` never touches the database.

## Backups

SQLite runs in WAL mode, so a plain `cp` of `steelshelf.db` can miss committed
pages. Copy it with SQLite's online backup instead, then copy `photos/` as
files:

```sh
sqlite3 data/steelshelf.db ".backup 'backup/steelshelf.db'"
rsync -a --exclude _derived --exclude _drafts data/photos/ backup/photos/
```

`photos/_derived/` (thumbnails and spine strips) and `photos/_drafts/`
(unsaved uploads, pruned after a day) are remade or disposable. To restore,
stop the container, delete any `steelshelf.db-wal` and `-shm` beside the
database, copy the backup in, and start it again.

## The optional Claude Code worker

Identification uses the Anthropic API key by default, at cents per item. If
you have Claude Code logged in on a machine, `tools/worker.py` can answer
instead on your own subscription, and the app falls back to the API whenever
the worker is off or fails:

1. On that machine, clone the repo, create its `.venv`, and write
   `~/.config/steelshelf-worker.env` (mode 600) with
   `STEELSHELF_WORKER_SECRET` (`openssl rand -hex 32`), `CLAUDE_BIN`
   (`command -v claude`), and `STEELSHELF_WORKER_HOST=0.0.0.0` if the app runs
   on another machine.
2. Install `tools/steelshelf-worker.service` as a systemd user unit; the
   worker's docstring has the commands.
3. In the app's `.env`, set `WORKER_URL` (`http://<worker host>:8012`) and
   `WORKER_SECRET` to the same secret, then recreate the container.

The same worker also judges listings for pricing (`POST /judge`) when the
app's `JUDGE_LISTINGS` is on; pull and restart it after an upgrade so it has
the app's current prompts and schemas.

The worker runs Claude Code headless with only Read (confined to the photos)
and, for identify, WebSearch. It uses your own `claude` login on your own machine; whether
that use fits your plan's terms is between you and Anthropic. The app never
holds the subscription's credentials.
