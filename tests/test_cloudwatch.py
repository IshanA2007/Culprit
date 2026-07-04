"""Task 8 — CloudWatch logs provider + log parsing (plan decision 8).

Stack-trace source order (HANDOFF §4): webhook frames -> Sentry API -> **logs** ->
genuinely absent. ``culprit/logparse.py`` turns the middleware's stderr exception
JSON into the same frames shape the ranking composite already consumes (so a
log-derived frame ranks exactly like a Sentry frame), and gunicorn markers into
infra-class markers. ``culprit/cloudwatch.py`` serves those logs behind a provider
interface: ``FixtureLogsProvider`` over the 22 committed captures (offline);
``Boto3LogsProvider`` gated on AWS creds (inert here).
"""

from __future__ import annotations

from pathlib import Path

from culprit.cloudwatch import Boto3LogsProvider, FixtureLogsProvider
from culprit.config import REPO_ROOT
from culprit.logparse import (
    distinct_client_ips,
    error_line_count,
    gunicorn_markers,
    parse_error_events,
    parse_traceback_frames,
)

LOGS_DIR = REPO_ROOT / "fixtures" / "logs"


def _log(fault_substr: str) -> Path:
    return next(p for p in sorted(LOGS_DIR.glob("*.log")) if fault_substr in p.name)


# --- traceback -> frames (normalized to match the Sentry twin) ---------------


def test_traceback_frames_are_normalized_and_in_app_only():
    tb = (
        "Traceback (most recent call last):\n"
        '  File "/opt/venv/lib/python3.12/site-packages/django/core/handlers/base.py", line 55, in inner\n'
        '  File "/app/tcf_website/views/courses/course_instructor.py", line 178, in course_instructor\n'
        "django.urls.exceptions.NoReverseMatch: Reverse for 'instructor_detail' not found."
    )
    frames = parse_traceback_frames(tb)
    files = [f["file"] for f in frames]
    # site-packages/django frames dropped; /app/ prefix stripped to fork-relative
    assert files == ["tcf_website/views/courses/course_instructor.py"]
    assert frames[0]["lineno"] == 178
    assert frames[0]["function"] == "course_instructor"


def test_log_frames_match_the_sentry_twin_files():
    text = _log("template-noreversematch-instructor-card-w1").read_text()
    events = parse_error_events(text)
    assert events, "expected ERROR events in the log"
    frame_files = {f["file"] for e in events for f in e.frames}
    # same fork-relative file the Sentry event_alert cites -> ranks identically
    assert "tcf_website/views/courses/course_instructor.py" in frame_files


def test_silent_n_plus_one_log_yields_frames():
    # n-plus-one is silent to Sentry but its log DOES carry a traceback -> frames
    text = _log("n-plus-one-section-instructor-prefetch-w1").read_text()
    events = parse_error_events(text)
    files = {f["file"] for e in events for f in e.frames}
    assert any(f.startswith("tcf_website/") for f in files)


# --- gunicorn / infra markers ------------------------------------------------


def test_gunicorn_markers_detected():
    text = (
        "[2026-07-04 04:05:23 +0000] [1] [CRITICAL] WORKER TIMEOUT (pid:8)\n"
        "[2026-07-04 04:05:24 +0000] [1] [ERROR] Worker (pid:8) was sent SIGKILL! "
        "Perhaps out of memory?\n"
        "[2026-07-04 04:05:24 +0000] [1] [INFO] Booting worker with pid: 20\n"
    )
    markers = gunicorn_markers(text)
    assert "worker_timeout" in markers
    assert "worker_sigkill" in markers


def test_markerless_boot_only_log_has_no_markers():
    text = _log("gunicorn-worker-oom-w3").read_text()
    # the recorded OOM log captured only boot lines (documented honestly)
    assert gunicorn_markers(text) == []
    assert parse_error_events(text) == []


# --- impact inputs from the logs ---------------------------------------------


def test_error_line_count_and_distinct_ips():
    text = _log("search-fielderror-500-w1").read_text()
    events = parse_error_events(text)
    assert error_line_count(events) == 6  # matches the recorded ERROR lines
    assert distinct_client_ips(events) == 1


# --- the provider interface --------------------------------------------------


async def test_fixture_logs_provider_reads_a_capture():
    provider = FixtureLogsProvider(_log("template-noreversematch-instructor-card-w1"))
    assert provider.enabled
    text = await provider.read()
    assert text and '"level": "ERROR"' in text


async def test_fixture_logs_provider_absent_file_is_inert():
    provider = FixtureLogsProvider(LOGS_DIR / "does-not-exist.log")
    assert provider.enabled is False
    assert await provider.read() is None


def test_boto3_logs_provider_inert_without_creds():
    provider = Boto3LogsProvider(region="us-east-1", log_groups=[], credentials=None)
    assert provider.enabled is False
