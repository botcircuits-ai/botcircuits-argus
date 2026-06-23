"""Username/password auth for the manager backend.

Credentials come from the environment — there is no user database:

    BOTCIRCUITS_MANAGER_ADMIN_USERNAME
    BOTCIRCUITS_MANAGER_ADMIN_PASSWORD

On successful login we mint a short, signed bearer token (HMAC-SHA256 over the
username + an expiry, keyed by a secret) that the web sends back on each
request. Stateless: no server-side session store, and no third-party JWT
dependency — just stdlib ``hmac``/``hashlib``. Tokens carry an expiry and are
constant-time verified.

The signing secret is ``BOTCIRCUITS_MANAGER_SECRET`` when set, else it is
derived from the admin password so a deployment that only sets the two admin
vars still gets stable, unguessable tokens (changing the password invalidates
old tokens, which is the desired behavior).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import time

USERNAME_ENV = "BOTCIRCUITS_MANAGER_ADMIN_USERNAME"
PASSWORD_ENV = "BOTCIRCUITS_MANAGER_ADMIN_PASSWORD"
SECRET_ENV = "BOTCIRCUITS_MANAGER_SECRET"

#: Default admin credentials when neither env var is set. Convenient for local
#: dev; override both vars in any real deployment.
DEFAULT_USERNAME = "admin"
DEFAULT_PASSWORD = "admin"

#: Token lifetime (seconds). 12h is a reasonable admin-console default.
TOKEN_TTL = 12 * 60 * 60


class AuthError(Exception):
    """Login failed or a token was invalid/expired."""


def _admin_username() -> str | None:
    return os.getenv(USERNAME_ENV, DEFAULT_USERNAME) 


def _admin_password() -> str | None:
    return os.getenv(PASSWORD_ENV, DEFAULT_PASSWORD) 


def is_configured() -> bool:
    """Always True: the manager falls back to the default ``admin``/``admin``
    credentials when the env vars are unset, so authentication is always
    available (override both vars in any real deployment)."""
    return bool(_admin_username() and _admin_password())


def _secret() -> bytes:
    explicit = os.getenv(SECRET_ENV)
    if explicit:
        return explicit.encode("utf-8")
    # Derive from the password so tokens are stable per-deployment without a
    # separate secret, and rotate automatically when the password changes.
    pw = _admin_password() or ""
    return hashlib.sha256(("botcircuits-manager:" + pw).encode("utf-8")).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(s: str) -> bytes:
    pad = "=" * (-len(s) % 4)
    return base64.urlsafe_b64decode(s + pad)


def login(username: str, password: str) -> str:
    """Verify credentials and return a signed bearer token.

    Raises :class:`AuthError` on bad credentials or when no admin is
    configured. Comparisons are constant-time.
    """
    admin_user, admin_pw = _admin_username(), _admin_password()
    if not admin_user or not admin_pw:
        raise AuthError("manager admin credentials are not configured")
    ok_user = hmac.compare_digest(username or "", admin_user)
    ok_pw = hmac.compare_digest(password or "", admin_pw)
    if not (ok_user and ok_pw):
        raise AuthError("invalid username or password")
    return _mint(admin_user)


def _mint(username: str) -> str:
    exp = int(time.time()) + TOKEN_TTL
    payload = f"{username}:{exp}".encode("utf-8")
    sig = hmac.new(_secret(), payload, hashlib.sha256).digest()
    return f"{_b64(payload)}.{_b64(sig)}"


def verify(token: str) -> str:
    """Return the username for a valid, unexpired token, else raise
    :class:`AuthError`."""
    if not token or "." not in token:
        raise AuthError("malformed token")
    payload_b64, sig_b64 = token.split(".", 1)
    try:
        payload = _unb64(payload_b64)
        sig = _unb64(sig_b64)
    except Exception as e:  # noqa: BLE001
        raise AuthError("malformed token") from e
    expected = hmac.new(_secret(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(sig, expected):
        raise AuthError("bad signature")
    try:
        username, exp_s = payload.decode("utf-8").rsplit(":", 1)
        exp = int(exp_s)
    except Exception as e:  # noqa: BLE001
        raise AuthError("malformed token payload") from e
    if time.time() > exp:
        raise AuthError("token expired")
    return username


__all__ = [
    "AuthError",
    "is_configured",
    "login",
    "verify",
    "TOKEN_TTL",
    "USERNAME_ENV",
    "PASSWORD_ENV",
    "SECRET_ENV",
]
