"""Task 7 — synthesized postmortem-input fixtures (rollback deploy + chat thread).

Deployfeed-grade: the fix-deploy is a genuine ``workflow_run`` shipping ``base_sha``
(the rollback target — a real fork commit) after the fault shipped; only code
faults get one. The Discord thread is generic on-call chatter that never names the
fault or the culprit sha (anti-leakage). Both regenerate byte-stable.
"""

from __future__ import annotations

import json

from culprit.discord_read import FixtureThreadReader
from culprit.ingest.github import parse_github
from harness.deployfeed import iso_from_compact, load_inputs
from harness.discordfeed import build_fix_deploy_envelope, build_thread_fixture
from harness.runrecord import load_all_run_records

RUNS = load_all_run_records()
CODE = [r for r in RUNS if r.fault_class == "code"]
INFRA = [r for r in RUNS if r.fault_class == "infra"]
INCIDENT = [r for r in RUNS if r.ground_truth != "no_incident"]


def _inputs():
    return load_inputs()[0]


def test_fix_deploy_ships_base_sha_for_code_faults():
    run = CODE[0]
    env = build_fix_deploy_envelope(run, _inputs())
    body = json.loads(env["raw_body"].encode("latin-1"))
    parsed = parse_github(body)
    assert parsed is not None
    assert parsed.head_sha == run.base_sha  # rollback to last-known-good
    assert parsed.conclusion == "success"
    assert env["reconstructed"] is True


def test_fix_deploy_run_started_after_injection():
    run = CODE[0]
    env = build_fix_deploy_envelope(run, _inputs())
    body = json.loads(env["raw_body"].encode("latin-1"))
    started = body["workflow_run"]["run_started_at"]
    assert started > iso_from_compact(run.injected_at)  # the fix ships after the fault


def test_fix_deploy_regeneration_is_byte_stable():
    run = CODE[0]
    inp = _inputs()
    assert build_fix_deploy_envelope(run, inp) == build_fix_deploy_envelope(run, inp)


def test_fix_deploy_names_no_fault_or_culprit():
    run = CODE[0]
    blob = json.dumps(build_fix_deploy_envelope(run, _inputs()))
    assert run.fault_id not in blob  # never names the fault
    if run.culprit_sha:
        assert run.culprit_sha not in blob  # never names the culprit sha


async def test_thread_is_generic_and_readable(tmp_path):
    run = INCIDENT[0]
    fixture = build_thread_fixture(run)
    blob = json.dumps(fixture)
    assert run.fault_id not in blob  # generic chatter — no fault identity
    if run.culprit_sha:
        assert run.culprit_sha not in blob
    assert fixture["reconstructed"] is True

    path = tmp_path / "thread.json"
    path.write_text(blob)
    msgs = await FixtureThreadReader(str(path)).read()
    assert msgs and all(m["content"] and m["author"] for m in msgs)


def test_thread_regeneration_is_byte_stable():
    run = INCIDENT[0]
    assert build_thread_fixture(run) == build_thread_fixture(run)
