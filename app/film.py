"""Film facts from TMDB: a title in, its main genre and its director out.

The shelf's "By genre" and "By director" sorts group on these. `Tmdb` searches
The Movie Database by title and picks a match (`pick`), then reads that film's
first listed genre and its director(s) from the credits.

An item's `tmdb_id` records the lookup: NULL = not looked up yet, 0 = TMDB had
no match, -1 = typed by hand (`db.HAND_SET`), which no lookup or refetch
touches. `fill_items` fills `genre` and `director` only where they are blank. A request that fails leaves the item
NULL, to be tried again on the next save or startup.

Nothing here runs on a page render: the callers are a background thread after
a save and one at startup.
"""

import logging
import re
import sqlite3
from dataclasses import dataclass
from typing import Protocol

import httpx

from app import db

log = logging.getLogger("steelshelf.film")

API = "https://api.themoviedb.org/3"
# A v3 API key is 32 hex characters; anything else is taken as a v4 read token.
V3_KEY = re.compile(r"[0-9a-f]{32}")


_NOT_WORD = re.compile(r"[^0-9a-z]+")


def same_title(a: str, b: str) -> bool:
    """Titles equal once case, punctuation and spacing are set aside."""
    return _NOT_WORD.sub("", (a or "").casefold()) == _NOT_WORD.sub("", (b or "").casefold())


def pick(title: str, results: list[dict]) -> dict | None:
    """The result to use: the most popular one titled exactly `title`, else TMDB's top.

    TMDB ranks a new release first, so "Toy Story" comes back as Toy Story 5 and
    "Spider-Man" as its latest sequel; the exact title finds the film on the case.
    A remake with the same title stays ambiguous, and is corrected on the edit form.
    """
    exact = [r for r in results
             if same_title(title, r.get("title")) or same_title(title, r.get("original_title"))]
    if exact:
        return max(exact, key=lambda r: r.get("popularity") or 0)
    return results[0] if results else None


class FilmError(Exception):
    """TMDB could not be asked (no key, network, HTTP error)."""


@dataclass(frozen=True)
class Film:
    tmdb_id: int
    genre: str | None
    director: str | None


class FilmSource(Protocol):
    def lookup(self, title: str) -> Film | None: ...


class Tmdb:
    """TMDB's v3 API. Either credential TMDB issues works: the v3 API key (sent as
    `api_key`) or the v4 read access token (a bearer)."""

    def __init__(self, key: str, http: httpx.Client | None = None):
        self.key = key
        self.http = http or httpx.Client(timeout=15.0)

    def lookup(self, title: str) -> Film | None:
        """The picked match's main genre and director(s); None when nothing matches."""
        results = self._get("/search/movie", {"query": title, "include_adult": "false"})
        match = pick(title, results.get("results") or [])
        if match is None:
            return None
        movie = self._get(f"/movie/{match['id']}", {"append_to_response": "credits"})
        genres = [g["name"] for g in movie.get("genres") or [] if g.get("name")]
        crew = (movie.get("credits") or {}).get("crew") or []
        # Co-directors (the Coens, the Wachowskis) share one plaque.
        directors = dict.fromkeys(c["name"] for c in crew
                                  if c.get("job") == "Director" and c.get("name"))
        return Film(int(movie["id"]), genres[0] if genres else None,
                    " & ".join(directors) or None)

    def _get(self, path: str, params: dict) -> dict:
        if not self.key:
            raise FilmError("TMDB key missing: set TMDB_API_KEY")
        headers = {}
        if V3_KEY.fullmatch(self.key):
            params = {**params, "api_key": self.key}
        else:
            headers["Authorization"] = f"Bearer {self.key}"
        try:
            resp = self.http.get(f"{API}{path}", params=params, headers=headers)
        except httpx.HTTPError as exc:
            raise FilmError(f"could not reach TMDB: {exc!r}") from exc
        if resp.status_code != 200:
            raise FilmError(f"TMDB: HTTP {resp.status_code} {resp.text[:200]}")
        return resp.json()


def fill_items(conn: sqlite3.Connection, source: FilmSource) -> int:
    """Look up every item not yet looked up; how many TMDB matched.

    Stops at the first failed request: the next one would most likely fail the
    same way, and the items left keep their NULL for the next run.
    """
    matched = 0
    for it in db.items_missing_film(conn):
        try:
            film = source.lookup(it["title"])
        except FilmError as exc:
            log.warning("film lookup stopped at item %d: %s", it["id"], exc)
            break
        db.set_film(conn, it["id"], film)
        conn.commit()
        matched += film is not None
    return matched
