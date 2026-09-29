from app import db


def test_index_renders(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "steelshelf" in r.text


def test_healthz_counts_items(client):
    r = client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "items": 0}


def test_schema_creates_three_tables(tmp_path):
    path = str(tmp_path / "s.db")
    db.init_db(path)
    with db.connect(path) as conn:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"items", "photos", "valuations"} <= names


def test_healthz_503_when_db_unreachable(client, monkeypatch):
    from app import settings as settings_module

    # A directory where the DB file should be: sqlite3 cannot open it.
    monkeypatch.setattr(settings_module.settings, "database_path", "/proc/not-a-db")
    r = client.get("/healthz")
    assert r.status_code == 503


def test_icons_and_manifest_are_served(client):
    for path, ctype in [
        ("/favicon.ico", "image/x-icon"),
        ("/static/icon.svg", "image/svg+xml"),
        ("/static/apple-touch-icon.png", "image/png"),
        ("/static/steelshelf.css", "text/css"),
        ("/static/fonts/fraunces-latin.woff2", "font/woff2"),
    ]:
        r = client.get(path)
        assert r.status_code == 200, path
        assert r.headers["content-type"].startswith(ctype), path
    manifest = client.get("/static/manifest.webmanifest").json()
    for icon in manifest["icons"]:
        assert client.get(icon["src"]).status_code == 200, icon["src"]


def test_pages_link_the_icon_and_no_cdn(client):
    r = client.get("/")
    assert 'rel="icon" href="/static/icon.svg"' in r.text
    assert 'rel="apple-touch-icon"' in r.text
    assert "https://" not in r.text  # page loads stay CDN-free


def test_header_shows_the_full_mark_and_the_tab_the_small_cut(client):
    r = client.get("/")
    assert '<img src="/static/mark.svg"' in r.text
    assert client.get("/static/mark.svg").headers["content-type"].startswith("image/svg+xml")
    icons = client.get("/static/manifest.webmanifest").json()["icons"]
    assert {"src": "/static/mark.svg", "sizes": "any", "type": "image/svg+xml"} in icons
