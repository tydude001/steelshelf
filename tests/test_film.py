"""TMDB film lookup: the client, filling items, and the genre and director shelves."""

import httpx
import pytest

from app import db, film, shelf
from app import main as main_module
from app.film import Film, FilmError, Tmdb

from tests.conftest import AUTH
from tests.test_items import post_item, rows
from tests.test_shelf import row

MOVIE = {
    "id": 348,
    "title": "Alien",
    "genres": [{"id": 27, "name": "Horror"}, {"id": 878, "name": "Science Fiction"}],
    "credits": {"crew": [
        {"name": "Ridley Scott", "job": "Director"},
        {"name": "Dan O'Bannon", "job": "Screenplay"},
    ]},
}


class FakeTmdb:
    """TMDB's search and movie endpoints; records each request."""

    def __init__(self, results=(348,), movie=MOVIE, status=200):
        self.results, self.movie, self.status = list(results), movie, status
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.status != 200:
            return httpx.Response(self.status, json={"status_message": "Invalid API key"})
        if request.url.path == "/3/search/movie":
            return httpx.Response(200, json={"results": [{"id": i} for i in self.results]})
        return httpx.Response(200, json=self.movie)


V3 = "0123456789abcdef0123456789abcdef"


def tmdb(fake, key="tok"):
    return Tmdb(key, http=httpx.Client(transport=httpx.MockTransport(fake)))


# --- the client -------------------------------------------------------------------


def test_lookup_takes_the_top_match_first_genre_and_director():
    fake = FakeTmdb(results=(348, 999))
    assert tmdb(fake).lookup("Alien") == Film(348, "Horror", "Ridley Scott")
    search, detail = fake.requests
    assert search.url.params["query"] == "Alien"
    assert detail.url.path == "/3/movie/348"
    assert detail.url.params["append_to_response"] == "credits"
    # A read access token rides in a header, never the URL.
    assert all(r.headers["authorization"] == "Bearer tok" for r in fake.requests)
    assert all("tok" not in str(r.url) for r in fake.requests)


def test_a_v3_api_key_goes_as_the_api_key_param():
    fake = FakeTmdb()
    assert tmdb(fake, key=V3).lookup("Alien").tmdb_id == 348
    assert all(r.url.params["api_key"] == V3 for r in fake.requests)
    assert all("authorization" not in r.headers for r in fake.requests)


def test_pick_prefers_the_exact_title_most_popular_first():
    results = [
        {"id": 5, "title": "Toy Story 5", "popularity": 900},
        {"id": 1, "title": "Toy Story", "popularity": 80},
        {"id": 2, "title": "Toy Story", "original_title": "Toy Story", "popularity": 3},
    ]
    assert film.pick("toy story", results)["id"] == 1
    assert film.pick("Once Upon a Time... in Hollywood",
                     [{"id": 7, "title": "Once Upon a Time in Hollywood"}])["id"] == 7
    # No exact title: TMDB's own top result.
    assert film.pick("Alien", results)["id"] == 5
    assert film.pick("Alien", []) is None


def test_lookup_uses_the_picked_result():
    class Search(FakeTmdb):
        def __call__(self, request):
            if request.url.path == "/3/search/movie":
                self.requests.append(request)
                return httpx.Response(200, json={"results": [
                    {"id": 999, "title": "Alien: Romulus", "popularity": 500},
                    {"id": 348, "title": "Alien", "popularity": 60}]})
            return super().__call__(request)

    fake = Search()
    assert tmdb(fake).lookup("Alien").tmdb_id == 348
    assert fake.requests[-1].url.path == "/3/movie/348"


def test_co_directors_share_one_name():
    movie = {"id": 1, "genres": [], "credits": {"crew": [
        {"name": "Joel Coen", "job": "Director"}, {"name": "Ethan Coen", "job": "Director"},
        {"name": "Joel Coen", "job": "Director"}]}}
    assert tmdb(FakeTmdb(movie=movie)).lookup("Fargo") == Film(1, None, "Joel Coen & Ethan Coen")


def test_no_match_is_none():
    assert tmdb(FakeTmdb(results=())).lookup("Nothing Like It") is None


def test_http_error_and_missing_token_raise():
    with pytest.raises(FilmError, match="HTTP 401"):
        tmdb(FakeTmdb(status=401)).lookup("Alien")
    fake = FakeTmdb()
    with pytest.raises(FilmError, match="TMDB_API_KEY"):
        tmdb(fake, key="").lookup("Alien")
    assert fake.requests == []


# --- filling items ----------------------------------------------------------------


class StubFilms:
    def __init__(self, answers):
        self.answers, self.asked = answers, []

    def lookup(self, title):
        self.asked.append(title)
        answer = self.answers[title]
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.fixture
def conn(tmp_path):
    path = str(tmp_path / "t.db")
    db.init_db(path)
    with db.connect(path) as c:
        yield c


def test_fill_writes_blanks_only_and_marks_no_match(conn):
    a = db.insert_item(conn, {"title": "Alien", "genre": "Sci-fi horror"})
    b = db.insert_item(conn, {"title": "Mystery"})
    source = StubFilms({"Alien": Film(348, "Horror", "Ridley Scott"), "Mystery": None})
    assert film.fill_items(conn, source) == 1
    alien, mystery = db.get_item(conn, a), db.get_item(conn, b)
    assert (alien["tmdb_id"], alien["genre"], alien["director"]) == (
        348, "Sci-fi horror", "Ridley Scott")  # the typed genre stands
    assert (mystery["tmdb_id"], mystery["genre"]) == (0, None)
    # Looked up once: a second run asks nothing.
    assert film.fill_items(conn, source) == 0
    assert source.asked == ["Alien", "Mystery"]


def test_a_failed_request_stops_the_run_and_leaves_items_to_retry(conn):
    a = db.insert_item(conn, {"title": "Alien"})
    b = db.insert_item(conn, {"title": "Heat"})
    source = StubFilms({"Alien": FilmError("down"), "Heat": Film(949, "Crime", "Michael Mann")})
    assert film.fill_items(conn, source) == 0
    assert source.asked == ["Alien"]
    assert db.get_item(conn, a)["tmdb_id"] is None and db.get_item(conn, b)["tmdb_id"] is None


# --- the shelves ------------------------------------------------------------------


def test_genre_and_director_group_largest_first_and_the_blank_last():
    items = [
        row(id=1, title="Alien", genre="Horror", director="Ridley Scott"),
        row(id=2, title="The Thing", genre="Horror", director="John Carpenter"),
        row(id=3, title="Blade Runner", genre="Science Fiction", director="Ridley Scott"),
        row(id=4, title="Heat"),
    ]
    by_genre = shelf.shelve(items, "genre")
    assert [s.name for s in by_genre] == ["Horror", "Science Fiction", shelf.NO_GENRE]
    assert [it["title"] for it in by_genre[0].items] == ["Alien", "The Thing"]
    by_director = shelf.shelve(items, "director")
    assert [s.name for s in by_director] == ["Ridley Scott", "John Carpenter", shelf.NO_DIRECTOR]
    assert [it["id"] for it in shelf.search(items, "carpenter")] == [2]


# --- the routes -------------------------------------------------------------------


@pytest.fixture
def films(admin, monkeypatch):
    """A stub TMDB, looked up synchronously so the test sees the result."""
    source = StubFilms({"Alien": Film(348, "Horror", "Ridley Scott"),
                        "Aliens": Film(679, "Action", "James Cameron")})
    monkeypatch.setattr(main_module.settings, "tmdb_api_key", "tok")
    monkeypatch.setattr(main_module, "_films", source)
    monkeypatch.setattr(main_module, "look_up_films", main_module.fill_films)
    return source


def test_saving_looks_the_film_up_and_the_shelf_groups_by_it(films, admin):
    item_id = int(post_item(admin).headers["location"].rsplit("/", 1)[1])
    assert rows("SELECT tmdb_id, genre, director FROM items")[0][:] == (
        348, "Horror", "Ridley Scott")
    body = admin.get("/?sort=director").text
    assert "Ridley Scott · 1" in body
    assert 'href="/?sort=genre"' in body
    page = admin.get(f"/items/{item_id}").text
    assert "https://www.themoviedb.org/movie/348" in page and "Ridley Scott" in page


def film_row():
    return rows("SELECT tmdb_id, genre, director FROM items")[0][:]


def test_retitling_refetches_what_the_old_title_filled(films, admin):
    item_id = int(post_item(admin).headers["location"].rsplit("/", 1)[1])
    data = {"title": "Aliens", "genre": "Horror", "director": "Ridley Scott"}  # as filled
    admin.post(f"/items/{item_id}", data=data, auth=AUTH)
    assert film_row() == (679, "Action", "James Cameron")


def test_typing_a_field_sets_the_item_by_hand_for_good(films, admin):
    item_id = int(post_item(admin).headers["location"].rsplit("/", 1)[1])
    data = {"title": "Aliens", "genre": "Horror", "director": "Jim"}  # director typed now
    admin.post(f"/items/{item_id}", data=data, auth=AUTH)
    assert film_row() == (db.HAND_SET, "Horror", "Jim")
    # A later retitle leaves it alone too.
    admin.post(f"/items/{item_id}", data={**data, "title": "Alien"}, auth=AUTH)
    assert film_row() == (db.HAND_SET, "Horror", "Jim")
    assert "set by hand" in admin.get(f"/items/{item_id}").text


def test_clearing_both_hands_the_item_back_to_tmdb(films, admin):
    loc = post_item(admin, data={"genre": "Mine"}).headers["location"]
    assert film_row() == (db.HAND_SET, "Mine", None)  # typed on the add form
    admin.post(loc, data={"title": "Alien", "genre": "", "director": ""}, auth=AUTH)
    assert film_row() == (348, "Horror", "Ridley Scott")


def test_stats_button_looks_every_film_up_again_but_the_hand_set(films, admin):
    post_item(admin)  # Alien, looked up
    post_item(admin, data={"title": "Aliens"})
    admin.post("/items/2", data={"title": "Aliens", "genre": "Typed", "director": "James Cameron"},
               auth=AUTH)
    body = admin.get("/stats").text
    assert "1 matched on TMDB" in body and "1 set by hand" in body
    assert 'action="/films/refetch"' in body
    assert admin.post("/films/refetch", auth=("admin", "wrong")).status_code == 401
    films.asked.clear()
    r = admin.post("/films/refetch", auth=AUTH, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/stats#films"
    assert films.asked == ["Alien"]
    got = rows("SELECT title, tmdb_id, genre, director FROM items ORDER BY id")
    assert [tuple(r) for r in got] == [("Alien", 348, "Horror", "Ridley Scott"),
                                       ("Aliens", db.HAND_SET, "Typed", "James Cameron")]


def test_the_refetch_button_needs_a_key(admin):
    post_item(admin)
    assert 'action="/films/refetch"' not in admin.get("/stats").text
    assert admin.post("/films/refetch", auth=AUTH).status_code == 404


def test_without_a_token_nothing_is_looked_up_and_typed_values_save(admin):
    loc = post_item(admin, data={"genre": "Horror", "director": "Ridley Scott"}).headers["location"]
    assert film_row() == (db.HAND_SET, "Horror", "Ridley Scott")
    admin.post(loc, data={"title": "Aliens", "genre": "Horror", "director": "Ridley Scott"},
               auth=AUTH)
    # No token: a retitle cannot refill, so nothing is cleared.
    assert rows("SELECT genre, director FROM items")[0][:] == ("Horror", "Ridley Scott")


def test_the_database_runs_in_wal_so_reads_never_wait_on_the_lookup(conn):
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
