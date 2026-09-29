"""HTTP Basic for every route that writes the library or calls a third party.

One username/password pair from the environment, compared in constant time. **It fails closed** — with
`ADMIN_USER` / `ADMIN_PASSWORD` unset, guarded routes answer 503 naming the two
variables, never the open door. Read-only pages stay open.

Basic auth is cleartext on the wire; the app is LAN-only and unencrypted
already, so this raises the bar from "anyone" to "anyone with the password".
"""

import logging
import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from app.settings import settings

log = logging.getLogger("steelshelf.auth")

_CHALLENGE = {"WWW-Authenticate": 'Basic realm="steelshelf"'}

# auto_error=False so a missing header reaches us: the unconfigured case must
# come back as 503, not as the 401 the default would raise first.
_basic = HTTPBasic(auto_error=False)


def _matches(given: str, expected: str) -> bool:
    return secrets.compare_digest(given.encode("utf-8"), expected.encode("utf-8"))


def require_admin(
    request: Request, credentials: HTTPBasicCredentials | None = Depends(_basic)
) -> str:
    """FastAPI dependency: the admin username, or a 401/503.

    Both halves are always compared, so timing does not say which was wrong.
    """
    if not settings.admin_user or not settings.admin_password:
        log.error("admin auth is not configured (%s)", request.url.path)
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "writes are disabled until ADMIN_USER and ADMIN_PASSWORD are set in .env",
        )
    if credentials is None:
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "login required", headers=_CHALLENGE
        )
    user_ok = _matches(credentials.username, settings.admin_user)
    pass_ok = _matches(credentials.password, settings.admin_password)
    if not (user_ok and pass_ok):
        log.warning("login rejected for %r on %s", credentials.username, request.url.path)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "bad credentials", headers=_CHALLENGE
        )
    return credentials.username
