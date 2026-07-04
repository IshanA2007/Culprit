"""Traffic driver — fires a fault's trigger requests at the running app.

Throttled to protect the Sentry free-tier quota (5k errors/month), and
auth-aware: faults marked ``requires_auth`` get a provisioned session cookie +
CSRF token (see ``harness/auth.py``) attached to every request.

Kept deterministic and countable (handoff §3): the number of requests fired and
their outcomes are recorded, so impact math downstream is exact, not estimated.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field

import httpx

from harness.config import HARNESS_HTTP_PORT
from harness.manifest import Fault


@dataclass
class RequestOutcome:
    path: str
    method: str
    status: int | None
    error: str | None = None


@dataclass
class TrafficResult:
    outcomes: list[RequestOutcome] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(
            1
            for o in self.outcomes
            if o.error is not None or (o.status is not None and o.status >= 500)
        )


def base_url(port: int = HARNESS_HTTP_PORT) -> str:
    return f"http://localhost:{port}"


def drive(
    fault: Fault,
    *,
    port: int = HARNESS_HTTP_PORT,
    cookies: dict[str, str] | None = None,
    csrf_token: str | None = None,
    repeats: int = 1,
    client: httpx.Client | None = None,
) -> TrafficResult:
    """Fire each of the fault's trigger requests ``repeats`` times, throttled."""
    owns_client = client is None
    client = client or httpx.Client(base_url=base_url(port), timeout=30.0)
    result = TrafficResult()
    try:
        for _ in range(repeats):
            for req in fault.trigger:
                headers = {}
                if csrf_token and req.method.upper() != "GET":
                    headers["X-CSRFToken"] = csrf_token
                try:
                    resp = client.request(
                        req.method,
                        req.path,
                        cookies=cookies,
                        headers=headers,
                    )
                    result.outcomes.append(
                        RequestOutcome(req.path, req.method, resp.status_code)
                    )
                except httpx.HTTPError as exc:  # noqa: PERF203 - per-request capture
                    result.outcomes.append(
                        RequestOutcome(req.path, req.method, None, str(exc))
                    )
                time.sleep(fault.throttle_seconds)
    finally:
        if owns_client:
            client.close()
    return result
