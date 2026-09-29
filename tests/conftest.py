import pytest
from fastapi.testclient import TestClient

from app import settings as settings_module

AUTH = ("admin", "pw")

# The suite starts from the code's defaults, never the working copy's .env: a
# developer's .env (or the README's `cp .env.example .env`) would otherwise set an
# admin login, real API keys and a worker URL under every test.
_defaults = settings_module.Settings(_env_file=None)
for _name in settings_module.Settings.model_fields:
    setattr(settings_module.settings, _name, getattr(_defaults, _name))


@pytest.fixture
def client(tmp_path, monkeypatch):
    """App wired to a throwaway SQLite DB and photo dir under tmp_path."""
    monkeypatch.setattr(settings_module.settings, "database_path", str(tmp_path / "t.db"))
    monkeypatch.setattr(settings_module.settings, "photo_dir", str(tmp_path / "photos"))
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin(client, monkeypatch):
    monkeypatch.setattr(settings_module.settings, "admin_user", AUTH[0])
    monkeypatch.setattr(settings_module.settings, "admin_password", AUTH[1])
    return client
