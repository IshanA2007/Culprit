"""Deterministic impact — exact counts, methodology stated on every number.

Plan decision 10 / HANDOFF §3: request counts are near-exact (server-side error
events, access logs), unique-user counts are estimates industry-wide. Culprit
computes both deterministically and **states the method** on each number; the LLM
never computes an impact number.

Source precedence:

* failed requests: Sentry ``issue.count`` (already ingested) -> error-line count
  in the CloudWatch logs over the incident window (Task 8) -> live ALB
  ``RequestCount``/5xx metrics (AWS-grant time).
* unique users: Sentry ``userCount`` (IP-keyed per Sentry docs) -> distinct
  client IPs in the logs (Task 8).

Each source carries its own method string so the brief is honest about how the
number was derived.
"""

from __future__ import annotations

from dataclasses import dataclass

# Method strings — the "how we counted" stated on every number.
SENTRY_REQ_METHOD = "Sentry issue.count (server-side error events, near-exact)"
LOGS_REQ_METHOD = "error lines in the CloudWatch logs over the incident window"
SENTRY_USER_METHOD = "Sentry userCount (IP-keyed per Sentry docs — an estimate)"
LOGS_USER_METHOD = "distinct client IPs in the logs (an estimate)"


@dataclass(frozen=True)
class ImpactMetric:
    """One measured number and the methodology it was derived from."""

    value: int
    method: str


@dataclass(frozen=True)
class Impact:
    """Deterministic impact snapshot for the brief + the M4 postmortem input."""

    failed_requests: ImpactMetric | None = None
    affected_users: ImpactMetric | None = None
    window: str | None = None

    def render(self) -> str:
        """The brief's Markdown impact line — states the method on each number."""
        over = f" over {self.window}" if self.window else ""
        fr = self.failed_requests
        if fr is not None:
            parts = [f"~{fr.value} failed request(s){over} (method: {fr.method})"]
        else:
            parts = [f"~0 failed request(s){over}"]
        au = self.affected_users
        if au is not None:
            parts.append(
                f"est. ≈{au.value} unique user(s) affected (method: {au.method})"
            )
        return "**Impact:** " + " · ".join(parts)

    def summary(self) -> str:
        """Plain-text one-liner for the LLM rationale prompt (no markdown)."""
        fr = self.failed_requests.value if self.failed_requests else 0
        out = f"~{fr} failed requests"
        if self.window:
            out += f" over {self.window}"
        if self.affected_users is not None:
            out += f"; ≈{self.affected_users.value} users (est.)"
        return out


def compute_impact(
    *,
    sentry_count: int | None = None,
    sentry_users: int | None = None,
    log_error_count: int | None = None,
    log_distinct_ips: int | None = None,
    window: str | None = None,
) -> Impact:
    """Assemble an Impact from the available sources, by precedence, deterministic."""
    failed: ImpactMetric | None = None
    if sentry_count:
        failed = ImpactMetric(int(sentry_count), SENTRY_REQ_METHOD)
    elif log_error_count:
        failed = ImpactMetric(int(log_error_count), LOGS_REQ_METHOD)

    users: ImpactMetric | None = None
    if sentry_users:
        users = ImpactMetric(int(sentry_users), SENTRY_USER_METHOD)
    elif log_distinct_ips:
        users = ImpactMetric(int(log_distinct_ips), LOGS_USER_METHOD)

    return Impact(failed_requests=failed, affected_users=users, window=window)
