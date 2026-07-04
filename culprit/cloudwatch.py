"""CloudWatch Logs behind a provider interface (plan decision 8).

The pipeline reads frameless-fault evidence (tracebacks the middleware logged when
Sentry was silent, gunicorn OOM/timeout markers) from logs. Two implementations
behind one ``LogsProvider`` protocol so the fixture path and the live path are
interchangeable:

* ``FixtureLogsProvider`` — reads one of the 22 committed captures in
  ``fixtures/logs/`` (offline; the eval + demo path);
* ``Boto3LogsProvider`` — CloudWatch Logs Insights via ``boto3`` in
  ``asyncio.to_thread`` (no blocking the event loop), **gated on AWS credentials**
  (absent -> inert). Zero live AWS is exercised in M3; this is the swap-in point
  for the AWS-grant upgrade (``docs/aws/aws-access.md``).
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Protocol


class LogsProvider(Protocol):
    """Serves raw log text for an incident's window (parsed by culprit.logparse)."""

    @property
    def enabled(self) -> bool: ...

    async def read(self) -> str | None: ...


class FixtureLogsProvider:
    """Reads one committed ``fixtures/logs/*.log`` capture (offline)."""

    def __init__(self, log_path: Path | str | None):
        self.log_path = Path(log_path) if log_path else None

    @property
    def enabled(self) -> bool:
        return self.log_path is not None and self.log_path.exists()

    async def read(self) -> str | None:
        if not self.enabled:
            return None
        return self.log_path.read_text()


class Boto3LogsProvider:
    """Live CloudWatch Logs Insights (gated on AWS creds; inert without).

    Runs the blocking boto3 client in ``asyncio.to_thread`` so it never blocks the
    event loop. Not exercised in M3 (zero live AWS) — the interface + gating are in
    place so ``docs/aws/aws-access.md``'s swap is a one-line change.
    """

    def __init__(
        self,
        *,
        region: str,
        log_groups: list[str],
        credentials: dict | None = None,
        query: str | None = None,
        start: int | None = None,
        end: int | None = None,
    ):
        self.region = region
        self.log_groups = log_groups
        self.credentials = credentials
        self.query = query or "fields @message | filter @message like /ERROR/"
        self.start = start
        self.end = end

    @property
    def enabled(self) -> bool:
        return bool(self.credentials and self.log_groups)

    async def read(self) -> str | None:
        if not self.enabled:
            return None
        return await asyncio.to_thread(self._query_sync)

    def _query_sync(self) -> str:  # pragma: no cover - requires live AWS
        import time

        import boto3

        client = boto3.client(
            "logs", region_name=self.region, **(self.credentials or {})
        )
        start = self.start or 0
        end = self.end or int(time.time())
        started = client.start_query(
            logGroupNames=self.log_groups,
            startTime=start,
            endTime=end,
            queryString=self.query,
        )
        qid = started["queryId"]
        while True:
            result = client.get_query_results(queryId=qid)
            if result["status"] in ("Complete", "Failed", "Cancelled"):
                break
            time.sleep(0.5)
        lines = [
            next((f["value"] for f in row if f["field"] == "@message"), "")
            for row in result.get("results", [])
        ]
        return "\n".join(lines)
