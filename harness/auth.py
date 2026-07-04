"""Authenticated-session provisioning without Cognito.

A few faults (e.g. ``vote-duplicate-integrityerror``) live behind
``@login_required`` + ``@require_POST`` views with CSRF protection. tCF's real
auth is Cognito (email OTP), which the local harness can't drive. Instead we
mint everything directly via ``manage.py shell`` inside the running web
container: get-or-create a User, build an authenticated session row, AND mint a
matching CSRF cookie/header pair (no page in tCF sets the ``csrftoken`` cookie on
a plain GET, so the traffic driver can't scrape one — we generate a valid pair
with ``get_token`` instead). The driver sends the ``sessionid`` + ``csrftoken``
cookies and the ``X-CSRFToken`` header.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from harness.config import HARNESS_WEB_CONTAINER

# Runs inside the web container. Works whether the custom User keys on username
# or email (USERNAME_FIELD), stamps the ModelBackend auth session keys, and mints
# a consistent CSRF cookie(secret)/header(masked token) pair.
_SHELL_SNIPPET = """
import json
from importlib import import_module
from django.conf import settings
from django.contrib.auth import get_user_model
from django.middleware.csrf import get_token
from django.test import RequestFactory

User = get_user_model()
uf = User.USERNAME_FIELD
value = "culprit-harness@example.com" if uf == "email" else "culprit-harness"
# computing_id has a unique constraint and defaults to "" (which a seeded user
# already holds), so give the harness user its own unique value.
defaults = {"email": "culprit-harness@example.com", "computing_id": "culpritharness"}
defaults = {k: v for k, v in defaults.items() if hasattr(User, k)}
defaults.pop(uf, None)
user, _ = User.objects.get_or_create(defaults=defaults, **{uf: value})
user.set_password("harness-not-secret")
user.is_active = True
user.save()

engine = import_module(settings.SESSION_ENGINE)
s = engine.SessionStore()
s["_auth_user_id"] = str(user.pk)
s["_auth_user_backend"] = "django.contrib.auth.backends.ModelBackend"
s["_auth_user_hash"] = user.get_session_auth_hash()
s.create()

# Mint a matching CSRF pair: the cookie holds the secret, the header holds a
# masked token derived from it; Django validates one against the other.
_req = RequestFactory().get("/")
_csrf_header = get_token(_req)
_csrf_cookie = _req.META.get("CSRF_COOKIE", "")

print("CULPRIT_SESSION=" + json.dumps({
    "sessionid": s.session_key,
    "username": str(getattr(user, uf)),
    "user_id": user.pk,
    "csrf_cookie": _csrf_cookie,
    "csrf_token": _csrf_header,
}))
"""


@dataclass
class Session:
    sessionid: str
    username: str
    user_id: int
    csrf_cookie: str = ""  # value of the csrftoken COOKIE (the secret)
    csrf_token: str = ""  # value for the X-CSRFToken HEADER (masked token)

    @property
    def cookies(self) -> dict[str, str]:
        c = {"sessionid": self.sessionid}
        if self.csrf_cookie:
            c["csrftoken"] = self.csrf_cookie
        return c


def provision_session(container: str = HARNESS_WEB_CONTAINER) -> Session:
    """Create a logged-in Django session + CSRF pair, bypassing Cognito."""
    proc = subprocess.run(
        [
            "docker",
            "exec",
            container,
            "python",
            "manage.py",
            "shell",
            "-c",
            _SHELL_SNIPPET,
        ],
        text=True,
        capture_output=True,
        check=True,
    )
    line = next(
        (ln for ln in proc.stdout.splitlines() if ln.startswith("CULPRIT_SESSION=")),
        None,
    )
    if not line:
        raise RuntimeError(
            f"session provisioning produced no marker:\n{proc.stdout}\n{proc.stderr}"
        )
    data = json.loads(line.removeprefix("CULPRIT_SESSION="))
    return Session(
        sessionid=data["sessionid"],
        username=data["username"],
        user_id=data["user_id"],
        csrf_cookie=data.get("csrf_cookie", ""),
        csrf_token=data.get("csrf_token", ""),
    )
