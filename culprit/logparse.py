"""Parse tCF's stderr logs into the shapes the pipeline already consumes.

theCourseForum's middleware dumps unhandled exceptions as JSON lines to stderr
(``{"level":"ERROR","path","method","ip","exception","message","traceback"}``) →
CloudWatch. This module turns those into:

* **frames** — the same ``{file, lineno, function}`` shape as Sentry in-app frames,
  normalized to fork-relative paths (``/app/tcf_website/...`` → ``tcf_website/...``,
  site-packages/django frames dropped) so a log-derived frame feeds the ranking
  composite *unchanged* (plan decision 9);
* **gunicorn infra markers** — ``WORKER TIMEOUT`` / ``SIGKILL`` boot churn, the
  app-too-dead-for-Sentry signal;
* **impact inputs** — error-line count + distinct client IPs (plan decision 10's
  logs source).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# `File "<path>", line <n>, in <func>` — one Python traceback frame.
_FRAME_RE = re.compile(r'File "([^"]+)", line (\d+), in (\S+)')
# The container working dir prefix the middleware logs; strip to fork-relative.
_APP_PREFIXES = ("/app/", "/opt/app/", "/code/")


def _normalize_path(path: str) -> str | None:
    """Fork-relative in-app path, or None for a non-in-app (library) frame."""
    for prefix in _APP_PREFIXES:
        if path.startswith(prefix):
            path = path[len(prefix) :]
            break
    # Only tcf_website/* is in-app source that maps to fork commits.
    return path if path.startswith("tcf_website/") else None


def parse_traceback_frames(traceback: str) -> list[dict]:
    """In-app frames from a Python traceback string (fork-relative, ordered)."""
    frames: list[dict] = []
    for match in _FRAME_RE.finditer(traceback or ""):
        file, lineno, func = match.groups()
        norm = _normalize_path(file)
        if norm:
            frames.append({"file": norm, "lineno": int(lineno), "function": func})
    return frames


@dataclass
class LogErrorEvent:
    path: str | None
    method: str | None
    ip: str | None
    exception: str | None
    message: str | None
    frames: list[dict] = field(default_factory=list)


def parse_error_events(text: str) -> list[LogErrorEvent]:
    """Parse the middleware's ``level=ERROR`` exception-JSON lines into events."""
    events: list[LogErrorEvent] = []
    for line in text.splitlines():
        line = line.strip()
        if not line.startswith("{") or '"level"' not in line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if obj.get("level") != "ERROR":
            continue
        events.append(
            LogErrorEvent(
                path=obj.get("path"),
                method=obj.get("method"),
                ip=obj.get("ip"),
                exception=obj.get("exception"),
                message=obj.get("message"),
                frames=parse_traceback_frames(obj.get("traceback", "")),
            )
        )
    return events


def gunicorn_markers(text: str) -> list[str]:
    """Infra-class markers from gunicorn stderr (worker timeout / OOM SIGKILL)."""
    markers: list[str] = []
    for line in text.splitlines():
        if "WORKER TIMEOUT" in line:
            markers.append("worker_timeout")
        if "SIGKILL" in line:
            markers.append("worker_sigkill")
    return markers


def distinct_client_ips(events: list[LogErrorEvent]) -> int:
    return len({e.ip for e in events if e.ip})


def error_line_count(events: list[LogErrorEvent]) -> int:
    return len(events)


def log_frames(events: list[LogErrorEvent]) -> list[dict]:
    """All in-app frames across the events (the pipeline's log-frame source)."""
    seen: set[tuple] = set()
    out: list[dict] = []
    for e in events:
        for f in e.frames:
            key = (f["file"], f["lineno"], f["function"])
            if key not in seen:
                seen.add(key)
                out.append(f)
    return out
