"""CloudWatch Synthetics canary: theCourseForum search smoke test.

This canary is the ONLY detector for the "search-silent-zero-results" fault:
search returns HTTP 200 with an empty result set and raises no exception, so it
is invisible to error monitoring (Sentry, 5xx alarms). The canary runs a
known-good query on a schedule and fails loudly when that query returns nothing,
which drops the Synthetics SuccessPercent metric and fires the
tcf-prod-search-canary CloudWatch alarm.

Detection logic (all three must hold, else the handler RAISES):
  1. the request returns HTTP 200;
  2. RESULT_MARKER is present in the response body (results were rendered);
  3. NO_RESULTS_MARKER is absent (the empty-state was NOT rendered).

Configuration comes from environment variables set by the Terraform run_config:
  SEARCH_URL         full URL including a query that must return results
  RESULT_MARKER      substring present only when >=1 result is shown
  NO_RESULTS_MARKER  substring present only on the empty-results state

REVIEWER: the two marker defaults below are conservative GUESSES. Confirm they
match theCourseForum's current search-results template (inspect the rendered
HTML for the results container / result-card class and for the empty-state
copy) and override them via the search_result_marker / search_no_results_marker
Terraform variables. A marker that never matches would make this canary either
always-pass (useless) or always-fail (noisy), so this check is load-bearing.
"""

import os
import urllib.request

# The Synthetics runtime provides aws_synthetics.common. Wrap the import so this
# module can also be imported/executed locally (e.g. in unit tests) where that
# package is not installed; fall back to the stdlib logger.
try:
    from aws_synthetics.common import synthetics_logger as logger
except ImportError:  # pragma: no cover - only taken outside the canary runtime
    import logging

    logger = logging.getLogger("search_smoke")
    logging.basicConfig(level=logging.INFO)

# Documented defaults. These are GUESSES -- see the REVIEWER note in the module
# docstring. Override with the Terraform variables rather than editing here.
DEFAULT_RESULT_MARKER = "search-result"
DEFAULT_NO_RESULTS_MARKER = "no-results"

# Generous but bounded: search should answer well under this even when warm-up
# or a cold cache is involved. A timeout is itself a failure worth alarming on.
REQUEST_TIMEOUT_SECONDS = 30


def _check_search() -> None:
    """Run the search assertions. Raises on any failure so the canary fails."""
    search_url = os.environ.get("SEARCH_URL")
    if not search_url:
        raise ValueError("SEARCH_URL environment variable is not set")

    result_marker = os.environ.get("RESULT_MARKER", DEFAULT_RESULT_MARKER)
    no_results_marker = os.environ.get("NO_RESULTS_MARKER", DEFAULT_NO_RESULTS_MARKER)

    logger.info("Requesting search URL: %s", search_url)

    request = urllib.request.Request(
        search_url,
        headers={"User-Agent": "tcf-search-smoke-canary"},
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        status = response.getcode()
        body = response.read().decode("utf-8", errors="replace")

    # 1. HTTP 200.
    if status != 200:
        raise AssertionError(f"Expected HTTP 200 from search, got {status}")

    # 2. Results marker present.
    if result_marker not in body:
        raise AssertionError(
            f"Result marker {result_marker!r} not found in response: a known-good "
            "query returned no results (silent zero-results fault)."
        )

    # 3. Empty-state marker absent.
    if no_results_marker in body:
        raise AssertionError(
            f"No-results marker {no_results_marker!r} present in response: search "
            "rendered its empty state for a known-good query."
        )

    logger.info("Search smoke test passed (HTTP 200, results present, no empty state).")


def handler(event, context):
    """Synthetics entry point (referenced as `search_smoke.handler`)."""
    _check_search()
    return "search smoke test passed"
