"""Authenticated-session provisioning without Cognito.

A few faults (e.g. ``vote-duplicate-integrityerror``) live behind
``@login_required`` views. tCF's real auth is Cognito (email OTP), which the
local harness can't drive. Instead we mint a Django session directly via
``manage.py shell`` inside the running web container: get-or-create a User, build
an authenticated session row, and hand the traffic driver the ``sessionid``
cookie. The CSRF token is obtained separately by the traffic driver (a GET that
sets the ``csrftoken`` cookie), since CSRF is double-submit, not session-bound.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass

from harness.config import HARNESS_WEB_CONTAINER

# Runs inside the web container. Works whether the custom User keys on username
# or email (USERNAME_FIELD), and stamps the ModelBackend auth session keys.
_SHELL_SNIPPET = """
import json
from importlib import import_module
from django.conf import settings
from django.contrib.auth import get_user_model

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
print("CULPRIT_SESSION=" + json.dumps({
    "sessionid": s.session_key,
    "username": str(getattr(user, uf)),
    "user_id": user.pk,
}))
"""


@dataclass
class Session:
    sessionid: str
    username: str
    user_id: int
    csrf_token: str = ""

    @property
    def cookies(self) -> dict[str, str]:
        c = {"sessionid": self.sessionid}
        if self.csrf_token:
            c["csrftoken"] = self.csrf_token
        return c


def provision_session(container: str = HARNESS_WEB_CONTAINER) -> Session:
    """Create a logged-in Django session in the harness DB, bypassing Cognito."""
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
        sessionid=data["sessionid"], username=data["username"], user_id=data["user_id"]
    )
