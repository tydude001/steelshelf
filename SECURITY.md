# Security

## Reporting a vulnerability

Please report security problems privately, through GitHub's private
vulnerability reporting: the repository's **Security** tab, then **Report a
vulnerability**. Don't open a public issue for them.

## The supported setup

steelshelf is built for a home network. Run it on a LAN, on a private
overlay network (Tailscale, WireGuard), or behind a reverse proxy that adds
TLS. **Reads are open by design**: every page and photo answers anyone who
can reach the port, and HTTP Basic sends the admin password in plain text
unless something in front of the app adds TLS. Putting the bare port on the
internet is outside what the app defends against.

## What counts

- **A write or a third-party call without auth.** Every route that changes
  the library, or that spends an API key (Anthropic, eBay, SerpApi, TMDB),
  needs the admin login, and fails closed with 503 while none is set. One
  that doesn't is a vulnerability.
- **A path outside `PHOTO_DIR`.** Photos are served and written by paths the
  app builds; a request that reads or writes a file outside the photo
  directory is a vulnerability.
- **An upload that runs code**, or that makes the image decoders do
  something other than decode an image.
- **The Claude Code worker reachable without its secret.** `tools/worker.py`
  answers only a bearer secret, and listens on `127.0.0.1` unless told
  otherwise. A way past either, or a way to make the Claude Code it runs use
  tools beyond reading the photos and searching the web, is a vulnerability.
- **A key leaking.** A way for the app to log, render or send an API key
  anywhere but the service it belongs to.

Not in scope: anything that needs an attacker to already run code as you, a
deployment that exposes the port to the internet without TLS, reads of the
library by someone who can reach the port (see above), and the third-party
services themselves.
