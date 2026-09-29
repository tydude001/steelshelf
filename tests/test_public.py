"""What a fresh clone promises: TMDB credited, a default password called out, data/ empty."""

import logging
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import settings as settings_module

ROOT = Path(__file__).resolve().parents[1]
CREDIT = "This product uses the TMDB API but is not endorsed or certified by TMDB."


def test_tmdb_is_credited_when_it_is_used(client, monkeypatch):
    monkeypatch.setattr(settings_module.settings, "tmdb_api_key", "tok")
    r = client.get("/")
    assert CREDIT in r.text and "/static/tmdb.svg" in r.text
    assert client.get("/static/tmdb.svg").status_code == 200


def test_no_tmdb_credit_without_a_key(client, monkeypatch):
    monkeypatch.setattr(settings_module.settings, "tmdb_api_key", "")
    assert CREDIT not in client.get("/").text


@pytest.fixture
def fresh(tmp_path, monkeypatch):
    monkeypatch.setattr(settings_module.settings, "database_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings_module.settings, "photo_dir", str(tmp_path / "photos"))
    monkeypatch.setattr(settings_module.settings, "tmdb_api_key", "")


def test_startup_warns_about_the_example_password(fresh, monkeypatch, caplog):
    monkeypatch.setattr(settings_module.settings, "admin_password", "changeme")
    from app.main import app

    with caplog.at_level(logging.WARNING, logger="steelshelf"), TestClient(app):
        pass
    assert any("changeme" in r.getMessage() for r in caplog.records)


def test_no_warning_for_a_real_password(fresh, monkeypatch, caplog):
    monkeypatch.setattr(settings_module.settings, "admin_password", "a-real-one")
    from app.main import app

    with caplog.at_level(logging.WARNING, logger="steelshelf"), TestClient(app):
        pass
    assert not any("changeme" in r.getMessage() for r in caplog.records)


@pytest.mark.skipif(not shutil.which("git") or not (ROOT / ".git").exists(),
                    reason="needs a git checkout")
def test_nothing_under_data_is_tracked_but_the_placeholder():
    tracked = subprocess.run(["git", "ls-files", "data"], cwd=ROOT, capture_output=True,
                             text=True, check=True).stdout.split()
    assert tracked == ["data/.gitkeep"]
