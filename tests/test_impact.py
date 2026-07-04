"""Task 3 — the deterministic impact calculator (plan decision 10).

Exact failed-request count + a hedged unique-user estimate, each tagged with the
methodology string it was derived from. Deterministic and reproducible: same
inputs -> same numbers, and the LLM computes no number (HANDOFF §3: request
counts near-exact, user counts are estimates industry-wide).
"""

from __future__ import annotations

from culprit.impact import Impact, compute_impact


def test_impact_is_deterministic():
    a = compute_impact(sentry_count=17, sentry_users=3)
    b = compute_impact(sentry_count=17, sentry_users=3)
    assert a == b
    assert a.failed_requests.value == 17
    assert a.affected_users.value == 3


def test_every_emitted_number_carries_a_method_string():
    imp = compute_impact(sentry_count=17, sentry_users=3)
    assert imp.failed_requests.method.strip()
    assert imp.affected_users.method.strip()
    # request count method names Sentry issue.count; user method flags the estimate
    assert "issue.count" in imp.failed_requests.method
    assert "estimate" in imp.affected_users.method.lower()


def test_sentry_source_takes_precedence_over_logs():
    imp = compute_impact(
        sentry_count=17, sentry_users=3, log_error_count=99, log_distinct_ips=42
    )
    assert imp.failed_requests.value == 17
    assert "Sentry" in imp.failed_requests.method
    assert imp.affected_users.value == 3


def test_falls_back_to_logs_source_with_its_own_method():
    imp = compute_impact(
        sentry_count=None, sentry_users=None, log_error_count=8, log_distinct_ips=4
    )
    assert imp.failed_requests.value == 8
    assert "log" in imp.failed_requests.method.lower()
    assert imp.affected_users.value == 4
    assert "ip" in imp.affected_users.method.lower()


def test_no_user_source_leaves_user_estimate_absent():
    imp = compute_impact(sentry_count=5, sentry_users=None)
    assert imp.failed_requests.value == 5
    assert imp.affected_users is None


def test_render_states_methodology():
    imp = compute_impact(sentry_count=17, sentry_users=3)
    line = imp.render()
    assert "~17 failed request" in line
    assert "≈3 unique user" in line
    assert "method:" in line  # methodology is stated on the number


def test_render_includes_window_when_known():
    imp = compute_impact(sentry_count=17, sentry_users=3, window="~4 min")
    assert "over ~4 min" in imp.render()


def test_summary_is_plain_text_for_the_llm_prompt():
    imp = compute_impact(sentry_count=17, sentry_users=3)
    s = imp.summary()
    assert isinstance(s, str) and "17" in s
    # plain text: no markdown bold markers the rationale prompt shouldn't echo
    assert "**" not in s


def test_empty_impact_renders_zero_without_crashing():
    imp = Impact()
    line = imp.render()
    assert "0 failed request" in line
