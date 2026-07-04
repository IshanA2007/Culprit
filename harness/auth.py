"""Authenticated-session provisioning without Cognito.

A few faults (e.g. ``vote-duplicate-integrityerror``) live behind
``@login_required`` views. tCF's real auth is Cognito (email OTP), which the
local harness can't drive. Instead we mint a session directly via
``manage.py shell`` inside the web container: create/get a User, build a Django
session, and hand the traffic driver the ``sessionid`` cookie + a CSRF token.

STATUS: skeleton. Implemented in Task 7 (decision: only one core fault depends on
this; demote that fault to stretch if it stalls >½ day — plan Risks table).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Session:
    sessionid: str
    csrf_token: str
    username: str

    @property
    def cookies(self) -> dict[str, str]:
        return {"sessionid": self.sessionid}


def provision_session(username: str = "culprit-harness") -> Session:
    """Create a logged-in Django session, bypassing Cognito, via manage.py shell."""
    raise NotImplementedError(
        "provision_session is implemented in Task 7 (needs the running web container)"
    )
